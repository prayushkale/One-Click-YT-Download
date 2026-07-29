API_KEY = ''
VOICE_LANGUAGE = 'English'

import warnings
# Suppress noisy deprecation/ssl warnings BEFORE any other imports
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*urllib3.*")
warnings.filterwarnings("ignore", message=".*Support for Python version.*")

import os
import sys
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import yt_dlp
import whisper
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Constants ──────────────────────────────────────────────────────────────
save_filename = "save.json"
MIN_AUDIO_SIZE_BYTES = 10_000  # files smaller than this are considered corrupt
MAX_PARALLEL_WORKERS = 3       # concurrent download+transcribe workers

# ── Globals (shared across threads) ────────────────────────────────────────
youtube = build('youtube', 'v3', developerKey=API_KEY)
save_lock = threading.Lock()   # thread-safe writes to save.json
whisper_model = None            # loaded once, shared across workers
whisper_model_lock = threading.Lock()
transcribe_lock = threading.Lock()  # serialize whisper calls (not fully thread-safe)

# ── Filesystem helpers ─────────────────────────────────────────────────────
def create_folder(folder_name):
    try:
        if not os.path.exists(folder_name):
            os.makedirs(folder_name)
            print(f"Folder '{folder_name}' created successfully.")
        else:
            print(f"Folder '{folder_name}' already exists.")
    except OSError as error:
        print(f"Error creating folder '{folder_name}': {error}")

def save_to_json(filename, data):
    with open(filename, 'w') as file:
        json.dump(data, file, indent=4)

def read_from_json(filename):
    try:
        with open(filename, 'r') as file:
            return json.load(file)
    except Exception:
        return None

# ── Progress persistence (thread-safe) ─────────────────────────────────────
def save_current_progress(channel_name, index):
    with save_lock:
        loaded_data = read_from_json(save_filename) or []
        new_data = []
        found = False
        for entry in loaded_data:
            if entry.get("channel_name") == channel_name:
                new_data.append({"channel_name": channel_name, "index": index})
                found = True
            else:
                new_data.append(entry)
        if not found:
            new_data.append({"channel_name": channel_name, "index": index})
        save_to_json(save_filename, new_data)

def find_channel_in_saved(items, channel_name):
    for item in (items or []):
        if item.get("channel_name") == channel_name:
            return item
    return None

# ── YouTube API helpers ────────────────────────────────────────────────────
def get_channel_id(username):
    try:
        search_response = youtube.search().list(
            q=username, type='channel', part='id'
        ).execute()
        if search_response.get('items'):
            return search_response['items'][0]['id']['channelId']
        return None
    except Exception as e:
        print(f"An error occurred: {e}")
        return None

def get_channel_content(api_key, channel_id):
    print(f"\nFetching Videos list, this could take some time. Please be patient...\n")
    all_videos = []
    try:
        channel_response = youtube.channels().list(
            part='contentDetails', id=channel_id
        ).execute()
        uploads_playlist_id = channel_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
        next_page_token = None

        print("Fetching videos list", end=" ", flush=True)
        while True:
            playlist_response = youtube.playlistItems().list(
                part='snippet',
                playlistId=uploads_playlist_id,
                maxResults=50,
                pageToken=next_page_token,
            ).execute()
            for item in playlist_response['items']:
                all_videos.append({
                    "title": item['snippet']['title'],
                    "published_at": item['snippet']['publishedAt'],
                    "video_id": item['snippet']['resourceId']['videoId'],
                    "thumbnail": item['snippet']['thumbnails']['high']['url']
                })
            print("*", end=" ", flush=True)
            next_page_token = playlist_response.get('nextPageToken')
            if not next_page_token:
                break
        print("Finished\n")
        return all_videos, len(all_videos)
    except HttpError as e:
        print(f'An HTTP error {e.resp.status} occurred:\n{e.content}')
        return None, None

# ── Download helpers ───────────────────────────────────────────────────────
def _build_ydl_opts(download_folder, video_index, mode='audio'):
    """Return yt-dlp options dict for audio or video download."""
    base_opts = {
        'outtmpl': os.path.join(download_folder, f'{video_index} - %(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': False,
        'socket_timeout': 60,
        'retries': 15,
        'fragment_retries': 15,
        'extractor_retries': 10,
        'continuedl': True,
        # Rate-limit to avoid triggering YouTube's bot detection
        'sleep_requests': 1,
        'sleep_interval': 3,
        'max_sleep_interval': 8,
        # Use multiple clients; tv+android+ios are more reliable than web right now
        'extractor_args': {
            'youtube': {
                'player_client': ['tv', 'android', 'ios', 'web'],
                # Skip web formats that need PO tokens (SABR); use only legacy + tv/android/ios
                'player_skip': ['web', 'web_safari'],
            }
        },
    }

    if mode == 'audio':
        # Prefer m4a (AAC), fall through to bestaudio, finally best (format 18 legacy combined)
        base_opts['format'] = (
            'bestaudio[ext=m4a]/bestaudio[ext=opus]/bestaudio[ext=webm]'
            '/bestaudio/best'
        )
        base_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }, {
            'key': 'FFmpegMetadata',
            'add_metadata': True,
        }]
    else:
        base_opts['format'] = (
            'bestvideo[ext=mp4]+bestaudio[ext=m4a]'
            '/best[ext=mp4]/bestvideo+bestaudio/best'
        )
        base_opts['merge_output_format'] = 'mp4'
        base_opts['postprocessors'] = [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }]

    return base_opts

def download_with_ydl(url, download_folder, video_index, mode='audio'):
    """
    Download a single video/audio. Returns (success, output_filename).
    On failure, cleans up any partial file and returns (False, error_message).
    """
    ydl_opts = _build_ydl_opts(download_folder, video_index, mode)
    ext = 'mp3' if mode == 'audio' else 'mp4'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return False, "yt-dlp returned None (possibly unavailable video)"

            video = info['entries'][0] if 'entries' in info else info
            filename = ydl.prepare_filename(video)
            output_basename = os.path.basename(os.path.splitext(filename)[0] + '.' + ext)
            output_path = os.path.join(download_folder, output_basename)

            # Sanity-check the output file
            if not os.path.exists(output_path):
                return False, f"Output file not found: {output_basename}"

            file_size = os.path.getsize(output_path)
            if mode == 'audio' and file_size < MIN_AUDIO_SIZE_BYTES:
                os.remove(output_path)
                return False, f"Audio file too small ({file_size} bytes) — likely corrupted"

            # Validate file is actually a media file (not an HTML error page).
            # mp3 files start with ID3 tag (49 44 33) or sync frame (FF FB/F3/F2/E3).
            # mp4 files start with ftyp box (00 00 00 xx 66 74 79 70).
            if file_size > 0:
                with open(output_path, 'rb') as f:
                    header = f.read(12)
                is_valid = False
                if ext == 'mp3':
                    # Check for ID3 tag or MPEG sync frame
                    if header[:3] == b'ID3' or (header[0] == 0xFF and (header[1] & 0xE0) == 0xE0):
                        is_valid = True
                elif ext == 'mp4':
                    # Check for ftyp box
                    if len(header) >= 8 and header[4:8] == b'ftyp':
                        is_valid = True

                if not is_valid:
                    os.remove(output_path)
                    return False, (
                        f"Downloaded file is not a valid {ext} file "
                        f"(got {header[:8].hex()}) — likely YouTube returned an error page"
                    )

            return True, output_basename

    except Exception as e:
        # Clean up any partial file that may have been written
        import glob
        pattern = os.path.join(download_folder, f'{video_index} - *')
        for f in glob.glob(pattern):
            try:
                os.remove(f)
            except OSError:
                pass
        return False, str(e)

# ── Transcription ──────────────────────────────────────────────────────────
def get_whisper_model():
    """Load whisper model once and share across threads."""
    global whisper_model
    with whisper_model_lock:
        if whisper_model is None:
            device = "cuda" if os.environ.get("CUDA_VISIBLE_DEVICES") else "cpu"
            model_size = os.environ.get("WHISPER_MODEL", "medium")
            print(f"Loading Whisper model '{model_size}' on {device} (one-time)...")
            whisper_model = whisper.load_model(model_size, device=device)
            print("Whisper model loaded.")
        return whisper_model

def transcribe_audio(audio_path, json_folder, output_basename):
    """Transcribe an audio file and save JSON result.
    Uses a lock because whisper is not fully thread-safe for concurrent calls."""
    lang_lower = VOICE_LANGUAGE.lower()
    lang_map = {
        "english": "en", "hindi": "hi", "tamil": "ta", "telugu": "te",
        "malayalam": "ml", "kannada": "kn", "bengali": "bn", "marathi": "mr",
        "gujarati": "gu", "punjabi": "pa", "urdu": "ur", "spanish": "es",
        "french": "fr", "german": "de", "japanese": "ja", "chinese": "zh",
        "korean": "ko", "arabic": "ar", "portuguese": "pt", "russian": "ru",
    }
    lang_code = lang_map.get(lang_lower, lang_lower)

    model = get_whisper_model()
    with transcribe_lock:
        result = model.transcribe(audio_path, language=lang_code, task="translate")

    json_path = os.path.join(json_folder, os.path.splitext(output_basename)[0] + ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    return json_path

# ── Parallel worker ────────────────────────────────────────────────────────
def process_one_video(args):
    """
    Process a single video: download audio (+ video if requested) and transcribe.
    Returns a dict with status info so the main loop can log results.
    """
    (video_index, video, video_folder, audio_folder, json_folder,
     download_option, channel_username) = args

    video_num = video_index + 1
    video_url = "https://www.youtube.com/watch?v=" + video['video_id']

    status = {
        'num': video_num,
        'title': video['title'],
        'video_downloaded': False,
        'audio_downloaded': False,
        'transcribed': False,
        'error': None,
    }

    want_video = download_option in (1, 2)
    want_audio = download_option in (1, 3, 4)
    want_transcribe = download_option in (1, 4)

    # ── Download video ──
    if want_video:
        ok, result = download_with_ydl(video_url, video_folder, video_num, mode='video')
        if ok:
            status['video_downloaded'] = True
        else:
            status['error'] = f"Video: {result}"

    # ── Download audio ──
    mp3_basename = None
    if want_audio:
        ok, result = download_with_ydl(video_url, audio_folder, video_num, mode='audio')
        if ok:
            status['audio_downloaded'] = True
            mp3_basename = result
        else:
            if not status['error']:
                status['error'] = f"Audio: {result}"

    # ── Transcribe ──
    if want_transcribe and mp3_basename and status['audio_downloaded']:
        audio_path = os.path.join(audio_folder, mp3_basename)
        try:
            # Validate the audio file exists and is non-trivial before transcribing
            if not os.path.exists(audio_path):
                if not status['error']:
                    status['error'] = f"Transcription: audio file missing: {mp3_basename}"
            elif os.path.getsize(audio_path) < MIN_AUDIO_SIZE_BYTES:
                if not status['error']:
                    status['error'] = (
                        f"Transcription: audio file too small "
                        f"({os.path.getsize(audio_path)} bytes)"
                    )
            else:
                json_path = transcribe_audio(audio_path, json_folder, mp3_basename)
                status['transcribed'] = True
        except Exception as e:
            # Don't overwrite a more specific download error
            if not status['error']:
                status['error'] = f"Transcription: {e}"

    # ── Determine overall success ──
    # Only save progress for videos that completed ALL requested operations
    if download_option == 1:
        overall_ok = (status['video_downloaded'] and status['audio_downloaded']
                      and status['transcribed'])
    elif download_option == 2:
        overall_ok = status['video_downloaded']
    elif download_option == 4:
        overall_ok = status['audio_downloaded'] and status['transcribed']
    else:
        # Option 3: Audio only
        overall_ok = status['audio_downloaded']

    # ── Save progress (only for successful operations so failed ones retry) ──
    if overall_ok:
        save_current_progress(channel_username, video_num)

    return status, overall_ok

# ── Main ───────────────────────────────────────────────────────────────────
def exit_program():
    print("\nExiting.....")
    time.sleep(1)
    sys.exit()

if __name__ == '__main__':
    channel_username = input("\nEnter Channel Name: ")
    channel_id = get_channel_id(channel_username)

    if not channel_id:
        print(f"\nYoutube Channel not found, please check if Channel name is correct")
        exit_program()

    all_videos, total_videos = get_channel_content(API_KEY, channel_id)
    if not all_videos or not total_videos:
        print(f"Did not find any Videos for the given Channel: {channel_username}")
        exit_program()
    print(f"Found {total_videos} Videos for Channel {channel_username}")

    pause_1 = input("\nDownload Now ? Press Enter to continue, type 'exit' to Exit: ")
    if pause_1 == 'exit':
        exit_program()

    # ── Progress resume ──
    progress_index = 0
    save_data = read_from_json(save_filename)
    if not save_data:
        print("\nNo save file found, creating Save file now...")
        save_to_json(save_filename, [])
    else:
        channel_progress = find_channel_in_saved(save_data, channel_username)
        if channel_progress:
            progress_index = channel_progress["index"]
            print(f"\nFound earlier progress for Channel: {channel_username}, "
                  f"Index: {progress_index}. Will continue from video {progress_index + 1}...")
    save_current_progress(channel_username, progress_index)

    # ── Download option ──
    while True:
        download_option = input(
            "\nSelect an Option: \n"
            "\t1. Download All (video, Audio, Transcripts) \n"
            "\t2. Videos Only \n"
            "\t3. Audio Only \n"
            "\t4. Audio+Transcripts\n\n=>"
        )
        try:
            download_option = int(download_option)
            if 1 <= download_option <= 4:
                break
        except ValueError:
            pass
        print("\nPlease select a valid option: 1 - 4")

    # ── Folder setup ──
    video_folder = f"{channel_username} Videos"
    audio_folder = f"{channel_username} Audios"
    json_folder = f"{channel_username} Data"

    if download_option in (1, 2):
        create_folder(video_folder)
    if download_option in (1, 3, 4):
        create_folder(audio_folder)
    if download_option in (1, 4):
        create_folder(json_folder)

    # ── Workers ──
    workers = input(f"\nParallel workers (1-8, default {MAX_PARALLEL_WORKERS}): ").strip()
    try:
        workers = int(workers) if workers else MAX_PARALLEL_WORKERS
        workers = max(1, min(8, workers))
    except ValueError:
        workers = MAX_PARALLEL_WORKERS
    print(f"Using {workers} parallel workers\n")

    # ── Sort oldest → newest ──
    all_videos.reverse()

    # ── Filter only pending videos ──
    pending_videos = [
        (i, v) for i, v in enumerate(all_videos)
        if (i + 1) > progress_index
    ]

    if not pending_videos:
        print("All videos already downloaded. Nothing to do.")
        exit_program()

    print(f"Downloading {len(pending_videos)} videos (skipping first {progress_index} already done)...\n")

    # ── Pre-load whisper model (one-time cost) ──
    if download_option in (1, 4):
        get_whisper_model()

    # ── Build task list ──
    tasks = [
        (i, v, video_folder, audio_folder, json_folder, download_option, channel_username)
        for i, v in pending_videos
    ]

    # ── Run in parallel ──
    completed = 0
    failed = 0
    total_pending = len(tasks)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_one_video, t): t for t in tasks}

        for future in as_completed(futures):
            status, overall_ok = future.result()
            completed += 1

            icon = "✓" if not status['error'] else "✗"
            parts = [f"[{completed}/{total_pending}] {icon} Video {status['num']}: {status['title'][:60]}"]
            if status['video_downloaded']:
                parts.append(" | video")
            if status['audio_downloaded']:
                parts.append(" | audio")
            if status['transcribed']:
                parts.append(" | transcript")
            if status['error']:
                parts.append(f" | ERROR: {status['error'][:80]}")
                failed += 1
            print("".join(parts))

    print(f"\n{'='*60}")
    print(f"Done! {completed} processed, {completed - failed} succeeded, {failed} failed.")
    print(f"Check '{audio_folder}/' and '{json_folder}/' for output.")
