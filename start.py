API_KEY = 'AIzaSyDMZhWcO4wf5Oer9jSnisITImYkEOwgLEc'

import os
import sys
import subprocess
import requests
import time
import json
import yt_dlp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

save_data = []
save_filename = "save.json"

youtube = build('youtube', 'v3', developerKey=API_KEY)

def create_folder(folder_name):
    try:
        # Check if the folder already exists
        if not os.path.exists(folder_name):
            # Create the folder
            os.makedirs(folder_name)
            print(f"Folder '{folder_name}' created successfully.")
        else:
            print(f"Folder '{folder_name}' already exists.")
    except OSError as error:
        print(f"Error creating folder '{folder_name}': {error}")

def save_to_json(filename, data):
    with open(filename, 'w') as file:
        json.dump(data, file, indent=4)
    print(f"\nProgress saved to {save_filename} File")

def read_from_json(filename):
    try:
        with open(filename, 'r') as file:
            loaded_data = json.load(file)
    except Exception as e:
        return None
    return loaded_data

def save_current_progress(channel_name, index):
    new_data = []
    loaded_data = read_from_json(save_filename)
    found_channel = False
    for data in loaded_data:
        if data["channel_name"] == channel_name:
            new_data.append({
                "channel_name": channel_name,
                "index": index
            })
            found_channel = True
        else:
            new_data.append(data)
    if not found_channel:
        new_data.append({
            "channel_name": channel_name,
            "index": index
        })
    save_to_json(save_filename, new_data)

def find_channel_in_saved(items, channel_name):
    for item in items:
        if item["channel_name"] == channel_name:
            return item
    return None

def get_channel_id(username):
    try:
        # Search for the channel
        search_response = youtube.search().list(
            q=username,
            type='channel',
            part='id'
        ).execute()

        # Check if any items were returned
        if 'items' in search_response and search_response['items']:
            return search_response['items'][0]['id']['channelId']
        else:
            return None
    except Exception as e:
        print(f"An error occurred: {e}")
        return None
    
def get_channel_content(api_key, channel_id):
    print(f"\nFetching Videos list, this could take some time. Please be patient...\n")

    total_videos = 0

    all_videos = []

    try:
        # Get channel uploads playlist ID
        channel_response = youtube.channels().list(
            part='contentDetails',
            id=channel_id
        ).execute()

        uploads_playlist_id = channel_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
        next_page_token = None

        print("Fetching videos list", end=" ", flush=True)
        while True:
            # Get playlist items
            playlist_response = youtube.playlistItems().list(
                part='snippet',
                playlistId=uploads_playlist_id,
                maxResults=50,
                pageToken=next_page_token,
            ).execute()

            # Add video details to the list
            for item in playlist_response['items']:
                all_videos.append(
                    {
                        "title":  item['snippet']['title'],
                        "published_at": item['snippet']['publishedAt'],
                        "video_id": item['snippet']['resourceId']['videoId'],
                        "thumbnail": item['snippet']['thumbnails']['high']['url']
                    }
                )
                total_videos += 1
            print("*", end=" ", flush=True)

            # Check if there are more pages
            next_page_token = playlist_response.get('nextPageToken')
            if not next_page_token:
                break
        print("Finished\n")

        return all_videos, total_videos

    except HttpError as e:
        print(f'An HTTP error {e.resp.status} occurred:\n{e.content}')
        return None, None

def get_caption_tracks(video_id):
    try:
        captions = youtube.captions().list(
            part='snippet',
            videoId=video_id
        ).execute()
        
        return captions.get('items', [])
    except Exception as e:
        # print(f"An error occurred: {e}")
        return []

def get_transcript(video_id):
    caption_tracks = get_caption_tracks(video_id)
    
    if not caption_tracks:
        print("No caption tracks found for this video.")
        return None
    
    # Usually, the first track is the auto-generated one
    first_track = caption_tracks[0]
    caption_id = first_track['id']
    
    # The download URL
    caption_url = f"https://youtube.googleapis.com/v3/captions/{caption_id}?alt=json&key={API_KEY}"
    
    try:
        response = requests.get(caption_url)
        response.raise_for_status()  # Raises an HTTPError for bad responses
        
        # The response is in SRT format
        return response.text
    except requests.exceptions.RequestException as e:
        # print(f"An error occurred while fetching the transcript: {e}")
        return None

def download_video(url, download_folder, video_index):
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': os.path.join(video_folder, f'{video_index} - %(title)s.%(ext)s'),
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
        'merge_output_format': 'mp4',
    }
            
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

        info = ydl.extract_info(url, download=True)
        if 'entries' in info:
            # It's a playlist, get the first video
            video = info['entries'][0]
        else:
            # It's a single video
            video = info
        
        # Construct the filename
        filename = ydl.prepare_filename(video)
        # Change the extension to mp4
        mp4_filename = os.path.basename(os.path.splitext(f"{video_index} - {filename}")[0] + '.mp4')
        
        return mp4_filename
    
def download_audio(url, download_folder, video_index):
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(download_folder, f'{video_index} - %(title)s.%(ext)s'),
        'postprocessors': [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            },
            {
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            },
        ],
    }
        
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

        info = ydl.extract_info(url, download=True)
        if 'entries' in info:
            # It's a playlist, get the first video
            video = info['entries'][0]
        else:
            # It's a single video
            video = info
        
        # Construct the filename
        filename = ydl.prepare_filename(video)
        # Change the extension to mp3
        mp3_filename = os.path.basename(os.path.splitext(f"{video_index} - {filename}")[0] + '.mp3')
        
        return mp3_filename
    
def exit():
    print("\nExiting.....")
    time.sleep(3)
    sys.exit()

if __name__ == '__main__':
    channel_username = input("\nEnter Channel Name: ")
    channel_id = get_channel_id(channel_username)

    if not channel_id:
        print(f"\nYoutube Channel not found, please check if Channel name is correct")
        exit()

    all_videos, total_videos = get_channel_content(API_KEY, channel_id)
    if all_videos and total_videos:
        print(f"Found {total_videos} Videos for Channel {channel_username}")
    else:
        print(f"Did not find any Videos for the given Channel: {channel_username}")
        exit()

    pause_1 = input("\nDownload Now ? Press Enter to continue, type 'exit' to Exit: ")
    if pause_1 == 'exit':
        exit()

    progress_index = 0
    save_data = read_from_json(save_filename)
    if not save_data:
        print("\nNo save file found, creating Save file now...")
        save_to_json(save_filename, [])
    else:
        channel_progress = find_channel_in_saved(save_data, channel_username)
        if channel_progress:
            progress_index = channel_progress["index"]
            print(f"\nFound earlier progress for Channel: {channel_username}, Index: {progress_index}. Will continue download...")

    save_current_progress(channel_username, progress_index)

    download_option = 0
    while True:
        download_option = input("\nSelect an Option: \n\t1. Download All (video, Audio, Transcripts) \n\t2. Videos Only \n\t3. Audio Only \n\t4. Audio+Transcripts\n\n=>")
        download_option = int(download_option)
        if download_option >= 1 and download_option <= 4:
            break
        else:
            print("\nPlease select a valid option: 1 - 4")

    video_folder = f"{channel_username} Videos"
    audio_folder = f"{channel_username} Audios"
    json_folder = f"{channel_username} Data"

    print("")
    if download_option == 1 or download_option == 2:
        create_folder(video_folder)
    if download_option == 1 or download_option == 3 or download_option == 4:
        create_folder(audio_folder)
    if download_option == 1 or download_option == 4:
        create_folder(json_folder)

    print("\nDownloading now ...\n")

    # sort by oldest to latest
    all_videos.reverse()

    for video_index, video in enumerate(all_videos):
        print(f"\nVideo {video_index+1}:")
        print("\tTitle: ", video['title'])
        print("\tPublished: ", video['published_at'])
        print("\tVideo ID: ", video['video_id'])
        print("\n")

        if (video_index + 1) > progress_index: 

            video_url = "https://www.youtube.com/watch?v=" + video['video_id']

            if download_option == 1 or download_option == 2:
                print("\nDownloading Video...\n")
                try:
                    mp4_filename = download_video(video_url, video_folder, video_index+1)
                except Exception as e:
                    print("Video not available yet: ", e)
                    continue
                print("\nVideo Downloaded...")

            if download_option == 1 or download_option == 3 or download_option == 4:
                print("\nDownloading Audio...\n")
                try:
                    mp3_filename = download_audio(video_url, audio_folder, video_index+1)
                except Exception as e:
                    print("Video not available yet: ", e)
                    continue
                print("\nAudio Downloaded...")

            if download_option == 1 or download_option == 4:
                transcript = get_transcript(video['video_id'])
                if transcript:
                    print("Transcript downloaded successfully.")
                    with open(f"{video_index+1} - {video['title']}_transcript.srt", "w", encoding="utf-8") as f:
                        f.write(transcript)
                else:
                    print("Failed to download transcript.")

                print("\nExtracting from Audio...")
                print("\nTranscribing ...\n")

                # Process audio to text
                command = f'whisper "{audio_folder}\\{mp3_filename}" --model medium --device cuda --language Hindi --task translate --output_dir "{json_folder}" --output_format json'
                # Execute the command using subprocess
                subprocess.run(command, shell=True, check=True)

            save_current_progress(channel_username, video_index+1)
        
        else:
            print("\nSkipping Video: ", video_index+1)
