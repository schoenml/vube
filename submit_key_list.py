#!/usr/bin/env python3
"""
submit_key_list.py

Submits a chosen list of page images already in S3 -- from any folders, any prefix --
to the Archivault pipeline as transcription batch jobs. For pages to transcribe again:
the same images, under the same keys, as were first sent, so each page comes back under
its own file name and can be put in the place of the earlier transcription.

The list is a text file of S3 keys, one per line (blank lines and lines starting with #
are ignored). Before anything is sent the script checks that every key is in the bucket,
that no two keys share a file name (the results are named by file name alone), and that
no key is already in a job of this ledger. The list is cut into jobs of at most
--max-per-job images (Archivault splits jobs much over 400-500 images, and a split job
takes two of the ~100 provider places); each job is titled <title>_partNN, which is
also the name collect_jobs.py gives its results.

Each job is written to the ledger in the same form as submit_volume_job.py's, so
check_job.py --pending and collect_jobs.py work on it unchanged.

Example:

    python submit_key_list.py --keys-file redo_01_keys.txt --title redo_01 \
        --source-bucket my-source-bucket --profile s3upload \
        --ledger submitted_jobs_redo01.jsonl --transcription-model gemini-3.8-flash \
        --email you@example.org --dry-run
"""

import os
import sys
import argparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError

try:
    from submit_job import login, submit_job, print_status
    import job_ledger
    from submit_volume_job import add_job_arguments, build_metadata, resolve_credentials
except ImportError as e:
    print(f"[!] Error: Could not import a required local module ({e}).")
    print("[!] Ensure submit_job.py, job_ledger.py and submit_volume_job.py are present.")
    sys.exit(1)


def read_keys(path):
    with open(path, encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip() and not line.startswith("#")]


def check_keys(keys, bucket, profile, ledger):
    """Every problem with the list, as lines to print; empty when it can be sent."""
    problems = []
    seen_keys, seen_names = set(), {}
    for key in keys:
        if key in seen_keys:
            problems.append(f"listed twice: {key}")
        seen_keys.add(key)
        name = key.rsplit("/", 1)[-1]
        if name in seen_names and seen_names[name] != key:
            problems.append(f"same file name as {seen_names[name]}: {key}")
        seen_names.setdefault(name, key)

    records, _malformed = job_ledger.read_ledger(ledger)
    for record in records:
        if record.get("status") in job_ledger.CLAIMED_STATUSES:
            for key in set(record.get("keys") or []) & seen_keys:
                problems.append(f"already in job {record.get('job_title')} of {ledger}: {key}")

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    s3 = session.client("s3")
    for key in sorted(seen_keys):
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except (BotoCoreError, ClientError) as e:
            problems.append(f"not found in the bucket ({e.__class__.__name__}): {key}")
    return problems


def main():
    parser = argparse.ArgumentParser(
        description="Submit a chosen list of S3 page images to Archivault as transcription batch jobs."
    )
    add_job_arguments(parser)
    # The keys carry their own prefixes (BP1/, BP2/, Daniel/ in one list), so the
    # shared --key-prefix is only recorded, not needed.
    for action in parser._actions:
        if action.dest == "key_prefix":
            action.required = False
            action.default = ""
    parser.add_argument("--keys-file", required=True, help="Text file of S3 keys, one per line")
    parser.add_argument("--title", required=True, help="Base job title, e.g. redo_01 (jobs are <title>_partNN)")
    parser.add_argument("--max-per-job", type=int, default=400, help="Most images in one job (default: 400)")
    parser.add_argument("--dry-run", action="store_true", help="Check and report the jobs, without submitting")
    args = parser.parse_args()

    if len(args.transcription_instructions) > 500:
        parser.error("--transcription-instructions must be at most 500 characters.")

    keys = read_keys(args.keys_file)
    if not keys:
        print(f"[!] No keys in '{args.keys_file}'.")
        sys.exit(1)
    print_status(f"{len(keys)} key(s) read from '{args.keys_file}'; checking them against the bucket and '{args.ledger}'...")
    problems = check_keys(keys, args.source_bucket, args.profile, args.ledger)
    if problems:
        print(f"[!] {len(problems)} problem(s); nothing submitted:")
        for line in problems:
            print(f"    {line}")
        sys.exit(1)

    parts = [keys[i:i + args.max_per_job] for i in range(0, len(keys), args.max_per_job)]
    titles = [f"{args.title}_part{n:02d}" for n in range(1, len(parts) + 1)]
    print_status(f"Every key found, once. {len(parts)} job(s), model {args.transcription_model}, batch mode:")
    for title, part in zip(titles, parts):
        print(f"    {title}: {len(part)} image(s)")
    print_status(f"Instructions: {args.transcription_instructions}")

    if args.dry_run:
        print_status("Dry run -- not submitting. Keys that would be sent:")
        for title, part in zip(titles, parts):
            for key in part:
                print(f"    {title}  {key}")
        return

    email, password = resolve_credentials(args)
    token = login(args.api_url, email, password)

    for title, part in zip(titles, parts):
        job_id, _artifacts, _upload, _inference, _final_title = submit_job(
            api_url=args.api_url,
            token=token,
            directory=None,
            files_to_upload=[],
            title=title,
            steps=["transcribe"],
            country=args.country,
            state=args.state,
            description=args.description,
            metadata=build_metadata(args),
            source_bucket=args.source_bucket,
            keys=part,
            batch_mode=True,
        )
        job_ledger.append_record(args.ledger, job_ledger.build_record(
            volume=f"{args.title}/{title}",
            job_title=title,
            keys=part,
            status=job_ledger.STATUS_SUBMITTED,
            job_id=job_id,
            api_url=args.api_url,
            source_bucket=args.source_bucket,
            key_prefix=args.key_prefix,
            steps=["transcribe"],
            transcription_model=args.transcription_model,
            batch_mode=True,
            delete_data=True,
            keys_file=args.keys_file,
            transcription_instructions=args.transcription_instructions,
        ))
        print_status(f"Submitted '{title}' ({len(part)} image(s)) as job {job_id}; recorded in '{args.ledger}'.")


if __name__ == "__main__":
    main()
