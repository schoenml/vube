import os
import re
import sys
import time
import argparse
import requests
import mimetypes
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote

DEFAULT_API_URL = "https://d2vqeenx44rrj7.cloudfront.net"

# Every artifact shares one basename (the job title, or the job ID when the job is
# untitled). Mirrors `file_basename` in aggregator_lambda/aggregator_handler.py.
ARTIFACT_SUFFIXES = {
    "json": ".json",
    "markdown": ".md",
    "tables_zip": "_tables.zip",
}

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_CD_FILENAME_STAR = re.compile(r"filename\*\s*=\s*(?:UTF-8'[^']*')?([^;]+)", re.I)
_CD_FILENAME = re.compile(r'filename\s*=\s*"([^"]*)"|filename\s*=\s*([^;]+)', re.I)
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Per-module model options. Must stay in sync with the `vocab` block in
# web-demo/presign_lambda/presign.py, which is the authoritative validator.
MODEL_OPTIONS = {
    # gemini-3.6-flash is retained only for transcription, so its OCR quality can be
    # compared against 3.7 on real material; 3.7's published gains are coding/agentic.
    # gemini-3.8-flash added 21 October 2026 (Mark): the Archivault site's default,
    # its name as the job record gives it (job 1550a23c).
    "transcription": [
        "gemini-3.1-pro-preview", "gpt-5.6-terra", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
        "gemini-3.5-flash-lite", "gpt-5.6-luna"
    ],
    "captioning": [
        "gemini-3.7-flash", "gpt-5.6-terra", "gemini-3.5-flash-lite", "gpt-5.6-luna"
    ],
    "foliation": [
        "gemini-3.7-flash", "gpt-5.6-terra", "gemini-3.5-flash-lite", "gpt-5.6-luna"
    ],
    "aggregation": [
        "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gpt-5.6-luna", "gemini-3.7-flash"
    ],
    "metadata": [
        "gemini-3.1-pro-preview", "gpt-5.6-terra", "gemini-3.7-flash", "gemini-3.5-flash-lite"
    ],
    "ner": [
        "gemini-3.7-flash", "gpt-5.6-terra", "gemini-3.5-flash-lite", "gpt-5.6-luna"
    ]
}

DEFAULT_METADATA_SCHEMA = {
    "title": "A concise human-readable name for the resource, suitable as a display title.",
    "creator": "The primary person, organization, or service responsible for creating the content of the resource.",
    "subject": "Key topics, themes, or keywords that describe what the resource is about.",
    "description": "A brief summary or abstract of the resource's content, including any notable features.",
    "publisher": "The person, organization, or service responsible for making the resource available.",
    "contributor": "Persons, organizations, or services who contributed to the content but are not the primary creator.",
    "date": "The most relevant date associated with the resource (e.g., creation, publication), formatted using ISO 8601/RFC 3339/RFC 9557 date syntax (e.g., '2025-12-02' or '2025-12-02T15:30:00Z') as precisely as known.",
    "type": "The general nature or genre of the resource (e.g., text, image, sound, dataset, map, moving image).",
    "format": "The physical or digital file format and/or medium of the resource (e.g., JPEG image, TIFF, PDF, audio file).",
    "identifier": "A unique string that identifies the resource within a collection or system (e.g., call number, local ID, URI).",
    "source": "Information about a related resource from which the current resource is derived or of which it is a part.",
    "language": "The primary language(s) of the resource's content, expressed using standardized BCP 47 language tags (e.g., 'en', 'es', 'pt-BR').",
    "relation": "References to related resources (e.g., isPartOf, hasPart, versionOf), described in brief natural language if no formal URI is known.",
    "coverage": "The spatial and/or temporal topic of the resource (e.g., place names, geographic regions, time periods).",
    "rights": "A concise statement of copyright, license, or usage conditions, or a note indicating that rights status is unknown."
}


def print_status(msg):
    print(f"[*] {msg}")

def login(api_url, email, password):
    print_status(f"Logging in as {email}...")
    resp = requests.post(f"{api_url}/auth/login", json={
        "email": email,
        "password": password
    })
    
    if not resp.ok:
        try:
            err = resp.json().get('error', resp.text)
        except Exception:
            err = resp.text
        print(f"[!] Login failed: {err}")
        sys.exit(1)
        
    data = resp.json()
    print_status(f"Login successful. Credits remaining: {data.get('creditsRemaining', 'Unknown')}")
    return data['token']

def get_files_to_upload(directory):
    if not os.path.isdir(directory):
        print(f"[!] Directory '{directory}' does not exist.")
        sys.exit(1)
        
    valid_exts = {'.pdf', '.jpg', '.jpeg', '.png', '.tif', '.tiff'}
    files_to_upload = []
    
    for filename in os.listdir(directory):
        filepath = os.path.join(directory, filename)
        if os.path.isfile(filepath):
            ext = os.path.splitext(filename)[1].lower()
            if ext in valid_exts:
                files_to_upload.append(filepath)
                
    if not files_to_upload:
        print(f"[!] No valid files (images/PDFs) found in {directory}")
        sys.exit(1)
        
    return files_to_upload

def _upload_single_file(item, path_map):
    filename = item['filename']
    upload_url = item['url']
    
    filepath = path_map.get(filename)
    if not filepath:
        print(f"[!] Warning: Presigned URL returned for unknown file {filename}")
        return
        
    content_type, _ = mimetypes.guess_type(filename)
    if not content_type:
        content_type = "application/octet-stream"
        
    max_retries = 5
    base_delay = 1.0  # seconds
    
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                print_status(f"Uploading {filename} (attempt {attempt}/{max_retries})...")
            else:
                print_status(f"Uploading {filename}...")
            
            with open(filepath, 'rb') as f:
                upload_resp = requests.put(
                    upload_url,
                    headers={"Content-Type": content_type},
                    data=f,
                    timeout=60
                )
            
            if upload_resp.ok:
                return
            
            # Check for transient errors (5xx)
            if upload_resp.status_code in [500, 502, 503, 504]:
                print(f"[!] S3 returned transient error status {upload_resp.status_code} during upload.")
            else:
                # Non-transient errors (e.g., 400, 403, 404, etc.)
                raise RuntimeError(f"Failed to upload {filename}: {upload_resp.status_code} - {upload_resp.text}")
                
        except requests.exceptions.RequestException as e:
            print(f"[!] Connection/network error during upload: {e}")
        
        if attempt < max_retries:
            delay = base_delay * (2 ** (attempt - 1))
            print_status(f"Retrying in {delay} seconds...")
            time.sleep(delay)
            
    raise RuntimeError(f"Failed to upload {filename} after {max_retries} attempts.")

def submit_job(api_url, token, directory, files_to_upload, title, steps, country, state, description, metadata, source_bucket=None, keys=None, batch_mode=False):
    headers = {"Authorization": f"Bearer {token}"}
    
    upload_start = time.time()
    # 1. Presign
    if source_bucket:
        print_status(f"Submitting import job for {len(keys)} keys from bucket '{source_bucket}'...")
        payload = {
            "job_title": title,
            "source_bucket": source_bucket,
            "filenames": keys,  # Collapse keys into filenames in the payload
            "steps": steps,
            "country": country,
            "state": state,
            "description": description,
            "metadata": metadata
        }
    else:
        print_status(f"Requesting presigned URLs for {len(files_to_upload)} files...")
        filenames = [os.path.basename(f) for f in files_to_upload]
        payload = {
            "job_title": title,
            "filenames": filenames,
            "steps": steps,
            "country": country,
            "state": state,
            "description": description,
            "metadata": metadata
        }
    
    resp = requests.post(f"{api_url}/presign", headers=headers, json=payload)
    if not resp.ok:
        print(f"[!] Presign/Import failed: {resp.text}")
        sys.exit(1)
        
    data = resp.json()
    job_id = data['jobId']
    presigned_urls = data.get('presignedUrls', [])
    pdf_count = data.get('pdf_count', 0)
    
    print_status(f"Job ID: {job_id}")
    
    # 2. Wait for copy to complete (only for S3 import flow)
    if source_bucket:
        print_status("Waiting for background S3 copy to complete...")
        while True:
            status_resp = requests.get(f"{api_url}/jobs/{job_id}", headers=headers)
            if not status_resp.ok:
                print(f"[!] Status check failed during S3 copy: {status_resp.text}")
                sys.exit(1)
                
            status_data = status_resp.json()
            st = status_data.get('status', '').upper()
            
            if st == 'IMPORTING':
                pass # Still copying in background
            elif st in ['PENDING', 'ENQUEUEING']:
                # Copy complete! Retrieve the actual pdf_count counted during background copy
                pdf_count = int(status_data.get('pdf_count') or 0)
                print_status(f"Background S3 copy completed! pdf_count = {pdf_count}")
                break
            elif st in ['FAILED', 'ERROR']:
                print(f"[!] S3 Copy Failed: {status_data.get('error', 'Unknown error')}")
                sys.exit(1)
                
            time.sleep(5)

    # 3. Upload (only for standard upload flow)
    if not source_bucket and presigned_urls:
        path_map = {os.path.basename(p): p for p in files_to_upload}
        print_status(f"Uploading {len(presigned_urls)} files in parallel...")
        max_workers = min(8, len(presigned_urls))
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_upload_single_file, item, path_map): item['filename'] for item in presigned_urls}
            for future in as_completed(futures):
                filename = futures[future]
                try:
                    future.result()
                except Exception as e:
                    print(f"[!] Critical upload error on file {filename}: {e}")
                    sys.exit(1)
            
    upload_duration = time.time() - upload_start
    inference_start = time.time()
            
    # 3. Handle PDFs if any
    if pdf_count > 0:
        print_status(f"Initializing PDF processing for {pdf_count} PDF(s)...")
        pdf_resp = requests.post(f"{api_url}/pdf", headers=headers, json={"jobId": job_id})
        if not pdf_resp.ok:
            print(f"[!] PDF initialization failed: {pdf_resp.text}")
            sys.exit(1)
            
        print_status("Waiting for PDFs to be processed...")
        while True:
            status_resp = requests.get(f"{api_url}/jobs/{job_id}", headers=headers)
            if not status_resp.ok:
                print(f"[!] Status check failed: {status_resp.text}")
                sys.exit(1)
                
            status_data = status_resp.json()
            st = status_data.get('status', '').upper()
            
            if st == 'ENQUEUEING':
                break
            elif st in ['FAILED', 'ERROR']:
                print(f"[!] PDF Processing Failed: {status_data.get('error', 'Unknown error')}")
                sys.exit(1)
                
            time.sleep(2)
            
    # 4. Enqueue Job
    print_status("Queueing job for processing...")
    enqueue_resp = requests.post(f"{api_url}/jobs", headers=headers, json={
        "jobId": job_id,
        "steps": steps
    })
    
    if not enqueue_resp.ok:
        print(f"[!] Enqueue failed: {enqueue_resp.text}")
        sys.exit(1)

    # Batch jobs turn around in hours, not minutes, so this script hands off at
    # enqueue rather than holding a foreground poll open for the whole window.
    # Artifacts are collected from the dashboard, or by a separate script polling
    # GET /jobs/{jobId} for status COMPLETED and reading its `artifacts` map.
    if batch_mode:
        print_status("Batch job submitted. Not waiting for completion.")
        print_status(f"Collect artifacts from the dashboard, or poll GET {api_url}/jobs/{job_id}")
        return job_id, None, upload_duration, None, ""

    # 5. Poll until completed
    print_status("Job is processing. Waiting for completion...")
    artifacts = None
    final_title = ""
    while True:
        status_resp = requests.get(f"{api_url}/jobs/{job_id}", headers=headers)
        if not status_resp.ok:
            print(f"[!] Status check failed: {status_resp.text}")
            sys.exit(1)

        status_data = status_resp.json()
        st = status_data.get('status', '').upper()

        if st == 'COMPLETED':
            print_status("Job completed successfully!")
            artifacts = status_data.get('artifacts', {})
            # Server-side title, already truncated to 32 chars by /presign. This is
            # the value the aggregator named the artifacts after.
            final_title = status_data.get('job_title', '') or ''
            break
        elif st in ['FAILED', 'ERROR']:
            print(f"[!] Job failed: {status_data.get('error', 'Unknown error')}")
            sys.exit(1)

        print_status(f"Status: {st}...")
        time.sleep(30)
    inference_duration = time.time() - inference_start
    return job_id, artifacts, upload_duration, inference_duration, final_title

def filename_from_content_disposition(header):
    """Pull the download filename out of a Content-Disposition header, if present."""
    if not header:
        return None

    match = _CD_FILENAME_STAR.search(header)
    if match:
        return unquote(match.group(1).strip().strip('"'))

    match = _CD_FILENAME.search(header)
    if match:
        return (match.group(1) or match.group(2) or "").strip()

    return None

def sanitize_filename(name, fallback="artifact"):
    """Make a server-supplied name safe to write to disk on any platform.

    Job titles are free text, so they can contain path separators or characters
    that are illegal on Windows. Browsers sanitize these when saving a download;
    a script writing straight to disk has to do the same.
    """
    name = _ILLEGAL_FILENAME_CHARS.sub("_", str(name or "").strip())
    name = os.path.basename(name).strip(" .")

    stem, ext = os.path.splitext(name)
    if stem.upper() in _WINDOWS_RESERVED:
        name = f"{stem}_{ext}"

    return name or fallback

def unique_path(output_dir, filename):
    """Return a path under output_dir that does not collide with an existing file."""
    stem, ext = os.path.splitext(filename)
    candidate = os.path.join(output_dir, filename)

    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(output_dir, f"{stem}_{counter}{ext}")
        counter += 1

    return candidate

def download_artifacts(artifacts, output_dir, job_title="", job_id=""):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Same rule the aggregator applies when it stamps the download names.
    basename = (job_title or "").strip() or job_id or "artifact"

    for key, info in artifacts.items():
        if isinstance(info, dict) and 'presigned_url' in info:
            url = info['presigned_url']
            if not url:
                continue

            resp = requests.get(url, stream=True)
            if not resp.ok:
                print(f"[!] Failed to download {key} artifact: {resp.status_code}")
                continue

            # The aggregator stamps each artifact with the filename the browser
            # uses, so prefer it and CLI downloads match what the UI produces.
            filename = filename_from_content_disposition(resp.headers.get('Content-Disposition'))
            if not filename:
                suffix = ARTIFACT_SUFFIXES.get(key, f"_{key}")
                filename = f"{basename}{suffix}"

            filename = sanitize_filename(filename, fallback=f"{job_id or 'artifact'}_{key}")
            filepath = unique_path(output_dir, filename)

            print_status(f"Downloading {key} artifact to {filepath}...")
            with open(filepath, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

def main():
    parser = argparse.ArgumentParser(description="Submit a job to Archivault via API")
    parser.add_argument("--dir", help="Local directory containing files to upload (required for local upload flow)")
    parser.add_argument("--source-bucket", help="External S3 bucket containing source files (required for S3 import flow)")
    parser.add_argument("--keys", nargs="+", help="List of S3 keys within source-bucket to import (required for S3 import flow)")
    parser.add_argument("--email", required=True, help="User email for authentication")
    parser.add_argument("--password", required=True, help="User password for authentication")
    parser.add_argument("--title", default="CLI Job", help="Job title")
    parser.add_argument("--steps", nargs="*", default=[], choices=["foliate", "metadata", "transcribe", "ner"], help="Processing steps (e.g. foliate transcribe)")
    parser.add_argument("--country", default="", help="Country of origin")
    parser.add_argument("--state", default="", help="State/Province")
    parser.add_argument("--description", default="", help="Job description")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Base Custom API URL")
    parser.add_argument("--out-dir", default="./output", help="Directory to save downloaded artifacts")

    # Metadata arguments
    parser.add_argument("--writing-style", default="", help="Writing style, e.g., handwritten, printed, typed")
    parser.add_argument("--language", default="", help="Language, e.g., english, spanish, italian, latin")
    parser.add_argument("--time-period", default="", help="Time period, e.g., contemporary, 19th_century_or_earlier")
    parser.add_argument("--layout-structure", default="", help="Layout structure, e.g., free_form, paragraphs")
    parser.add_argument("--transcription-model", default="gemini-3.7-flash", choices=MODEL_OPTIONS["transcription"], help="Transcription model")
    parser.add_argument("--captioning-model", default="gemini-3.5-flash-lite", choices=MODEL_OPTIONS["captioning"], help="Captioning model")
    parser.add_argument("--foliation-model", default="gemini-3.7-flash", choices=MODEL_OPTIONS["foliation"], help="Foliation model")
    parser.add_argument("--aggregation-model", default="gemini-3.5-flash-lite", choices=MODEL_OPTIONS["aggregation"], help="Aggregation model")
    parser.add_argument("--metadata-model", default="gemini-3.7-flash", choices=MODEL_OPTIONS["metadata"], help="Metadata generation model")
    parser.add_argument("--ner-model", default="gemini-3.7-flash", choices=MODEL_OPTIONS["ner"], help="Named entity recognition model")
    parser.add_argument("--metadata-schema", default=None, help="Path to a JSON file containing the metadata schema, or a raw JSON string")
    parser.add_argument("--context-file", default=None, help="Path to a local JSON file containing additional context per image (or name of S3-based context file in import flow)")
    parser.add_argument("--additional-context-modules", nargs="*", default=["foliation", "metadata", "transcription", "ner", "aggregation", "captioning", "layout"], help="List of modules to use the additional context file")
    parser.add_argument("--foliation-file", default=None, help="Path to a local foliation file (or name of S3-based foliation file in import flow)")
    parser.add_argument("--non-textual-elements", nargs="*", default=[], help="List of non-textual elements, e.g. illustrations, stamps_or_seals")
    parser.add_argument("--delete-data", action="store_true", help="Delete data after processing")
    parser.add_argument("--batch-mode", action="store_true", help="Submit captioning/transcription through the provider batch API instead of the live API (default: False). Cheaper, but turnaround is hours rather than minutes, so the script exits after enqueueing instead of waiting for artifacts.")
    parser.add_argument("--allow-subject-similarity", action="store_true", help="Allow foliation/aggregation to group images that merely share a subject or correspondent (default: False, i.e. group on physical characteristics so separate letters stay distinct)")
    
    # Transcription preferences
    parser.add_argument("--expand-abbreviations", action="store_true", help="Expand abbreviations (default: False)")
    parser.add_argument("--no-preserve-line-breaks", action="store_false", dest="preserve_line_breaks", help="Do not preserve line breaks (default: True)")
    parser.add_argument("--no-retain-punctuation", action="store_false", dest="retain_punctuation_and_spelling", help="Do not retain punctuation and spelling (default: True)")
    parser.add_argument("--normalize-to-modern", action="store_true", dest="normalize_to_modern_language", help="Normalize to modern language (default: False)")
    parser.add_argument("--ignore-marginalia", action="store_true", help="Ignore marginalia (default: False)")
    parser.add_argument("--transcription-instructions", default="", help="Custom project-specific transcription instructions (max 500 characters)")
    
    args = parser.parse_args()
    
    if not args.dir and not (args.source_bucket and args.keys):
        parser.error("Either --dir (local upload flow) or both --source-bucket and --keys (S3 import flow) must be provided.")
        
    files_to_upload = []
    image_files_count = 0
    
    if args.dir:
        image_files = get_files_to_upload(args.dir)
        files_to_upload = [os.path.abspath(f) for f in image_files]
        image_files_count = len(image_files)
    else:
        image_files_count = len(args.keys)
    
    additional_context_file = ""
    if args.context_file:
        if args.source_bucket:
            # S3 Import Flow: context file is expected to be copied as part of the keys
            additional_context_file = os.path.basename(args.context_file)
        else:
            if not os.path.isfile(args.context_file):
                print(f"[!] Context file '{args.context_file}' does not exist or is not a file.")
                sys.exit(1)
            abs_context_path = os.path.abspath(args.context_file)
            files_to_upload.append(abs_context_path)
            additional_context_file = os.path.basename(abs_context_path)
        
    foliation_file = ""
    steps = set(args.steps)
    foliation_override_discrete = False
    
    if args.foliation_file:
        if args.source_bucket:
            # S3 Import Flow: foliation file is expected to be copied as part of the keys
            foliation_file = os.path.basename(args.foliation_file)
            steps.add("foliate")
        else:
            if not os.path.isfile(args.foliation_file):
                print(f"[!] Foliation file '{args.foliation_file}' does not exist or is not a file.")
                sys.exit(1)
            abs_foliation_path = os.path.abspath(args.foliation_file)
            files_to_upload.append(abs_foliation_path)
            foliation_file = os.path.basename(abs_foliation_path)
            steps.add("foliate")
    elif "metadata" in steps and "foliate" not in steps:
        steps.add("foliate")
        foliation_override_discrete = True
        
    schema = DEFAULT_METADATA_SCHEMA
    if args.metadata_schema:
        if os.path.isfile(args.metadata_schema):
            try:
                with open(args.metadata_schema, 'r', encoding='utf-8') as f:
                    schema = json.load(f)
            except Exception as e:
                print(f"[!] Failed to parse metadata schema file: {e}")
                sys.exit(1)
        else:
            try:
                schema = json.loads(args.metadata_schema)
            except Exception as e:
                print(f"[!] Failed to parse metadata schema JSON string: {e}")
                sys.exit(1)
                
    if args.dir:
        print_status(f"Found {image_files_count} valid source files. Total files to upload: {len(files_to_upload)}")
    else:
        print_status(f"Referencing {image_files_count} files from source bucket '{args.source_bucket}' for import.")
    
    token = login(args.api_url, args.email, args.password)
    
    metadata = {
        "writing_style": args.writing_style,
        "language": args.language,
        "time_period": args.time_period,
        "layout_structure": args.layout_structure,
        "transcription_model": args.transcription_model,
        "captioning_model": args.captioning_model,
        "foliation_model": args.foliation_model,
        "aggregation_model": args.aggregation_model,
        "metadata_model": args.metadata_model,
        "ner_model": args.ner_model,
        "non_textual_elements": args.non_textual_elements,
        "transcription_preferences": {
            "expand_abbreviations": args.expand_abbreviations,
            "preserve_line_breaks": args.preserve_line_breaks,
            "retain_punctuation_and_spelling": args.retain_punctuation_and_spelling,
            "normalize_to_modern_language": args.normalize_to_modern_language,
            "ignore_marginalia": args.ignore_marginalia
        },
        "metadata_schema": schema,
        "additional_context_file": additional_context_file,
        "additional_context_modules": args.additional_context_modules,
        "foliation_file": foliation_file,
        "foliation_override_discrete": foliation_override_discrete,
        "allow_subject_similarity": args.allow_subject_similarity,
        "delete_data": args.delete_data,
        "batch_mode": args.batch_mode,
        "transcription_instructions": args.transcription_instructions
    }
    
    job_id, artifacts, upload_duration, inference_duration, final_title = submit_job(
        api_url=args.api_url,
        token=token,
        directory=args.dir,
        files_to_upload=files_to_upload,
        title=args.title,
        steps=list(steps),
        country=args.country,
        state=args.state,
        description=args.description,
        metadata=metadata,
        source_bucket=args.source_bucket,
        keys=args.keys,
        batch_mode=args.batch_mode
    )
    
    if args.batch_mode:
        print_status(f"Job {job_id} is in the batch queue. Submission complete.")
    elif artifacts:
        print_status("Downloading artifacts...")
        download_artifacts(
            artifacts,
            args.out_dir,
            job_title=final_title or args.title,
            job_id=job_id
        )
        print_status("Pipeline execution complete.")
    else:
        print_status("No artifacts were returned for this job.")

if __name__ == "__main__":
    main()
