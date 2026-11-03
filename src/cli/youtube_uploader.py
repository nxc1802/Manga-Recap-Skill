import argparse
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load env variables from project root .env
load_dotenv()

# Add project root to path
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.logger import logger
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

# Scopes required for YouTube Data API v3 upload
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

def get_credentials():
    """
    Retrieves and refreshes the YouTube OAuth2 credentials.
    Supports file-based token loading and manual browser flow.
    """
    secrets_file = os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", "client_secrets.json")
    token_file = os.getenv("YOUTUBE_CREDENTIALS_FILE", ".youtube_token.json")
    
    creds = None
    # 1. Try loading existing token
    if os.path.exists(token_file):
        try:
            logger.info(f"Loading credentials from token file: {token_file}")
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        except Exception as e:
            logger.warning(f"Failed to load credentials from token file: {e}")
            creds = None

    # 2. If token doesn't exist or is invalid, perform OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Credentials expired. Refreshing token...")
            try:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
            except Exception as e:
                logger.error(f"Failed to refresh credentials: {e}")
                creds = None
                
        if not creds:
            logger.info("Initializing new authentication flow...")
            if not os.path.exists(secrets_file):
                logger.error(
                    f"Client secrets file not found at: {secrets_file}\n"
                    "Please download OAuth client credentials from Google Cloud Console "
                    "and save them as client_secrets.json in the project root."
                )
                raise FileNotFoundError(f"Missing {secrets_file}")
                
            flow = InstalledAppFlow.from_client_secrets_file(secrets_file, SCOPES)
            creds = flow.run_local_server(port=0, authorization_prompt_message="Open this link in your browser to authorize:")
            
        # Save credentials for future runs
        try:
            with open(token_file, "w") as f:
                f.write(creds.to_json())
            logger.info(f"Credentials saved successfully to: {token_file}")
        except Exception as e:
            logger.warning(f"Could not save credentials to file: {e}")
            
    return creds

def upload_video(video_path, title, description, category_id="24", privacy_status="public", tags=None):
    """
    Uploads a video to YouTube using resumable chunked upload.
    """
    if not os.path.exists(video_path):
        logger.error(f"Video file not found at: {video_path}")
        sys.exit(1)
        
    logger.info(f"Initializing YouTube upload for: {video_path}")
    
    try:
        creds = get_credentials()
        youtube = build("youtube", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Authentication failed: {e}")
        sys.exit(1)

    tags_list = tags.split(",") if tags else ["Manga Recap", "Anime", "Webtoon"]
    tags_list = [t.strip() for t in tags_list]
    
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags_list,
            "categoryId": category_id
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False
        }
    }
    
    # Chunk size: 2MB (must be a multiple of 256KB)
    media = MediaFileUpload(
        video_path, 
        mimetype="video/mp4", 
        resumable=True, 
        chunksize=2*1024*1024
    )
    
    logger.info("Starting upload request to YouTube...")
    try:
        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media
        )
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                logger.info(f"Upload progress: {progress}% completed...")
                
        video_id = response.get("id")
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        
        logger.info("=========================================================")
        logger.info("SUCCESS: Video uploaded to YouTube successfully!")
        logger.info(f"Video ID: {video_id}")
        logger.info(f"Verify here: {video_url}")
        logger.info("=========================================================")
        
        return video_url
        
    except HttpError as e:
        logger.error(f"An HTTP error occurred: {e.resp.status} - {e.content.decode()}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"An unexpected error occurred during upload: {e}")
        sys.exit(1)

async def main():
    parser = argparse.ArgumentParser(description="Upload Video to YouTube via Data API v3")
    parser.add_argument("--video", type=str, required=True, help="Path to the video file to upload")
    parser.add_argument("--title", type=str, required=False, default=None, help="Title of the video")
    parser.add_argument("--description", type=str, default="Manga Recap video uploaded automatically.", help="Description of the video")
    parser.add_argument("--category", type=str, default="24", help="YouTube video category ID (default: 24 (Entertainment))")
    parser.add_argument("--privacy", type=str, default="public", choices=["public", "private", "unlisted"], help="Video privacy status (default: public)")
    parser.add_argument("--tags", type=str, default=None, help="Comma-separated tags for the video")
    args = parser.parse_args()

    title = args.title
    description = args.description
    privacy = args.privacy

    if sys.stdin.isatty():
        print("\n=== YouTube Upload Interactive Configuration ===")
        # Title prompting
        if not title:
            title = input("Enter YouTube Video Title: ").strip()
            while not title:
                title = input("Title cannot be empty. Enter YouTube Video Title: ").strip()
        else:
            confirm_title = input(f"Use title '{title}'? [Y/n]: ").strip().lower()
            if confirm_title in ('n', 'no'):
                title = input("Enter new YouTube Video Title: ").strip()
                while not title:
                    title = input("Title cannot be empty. Enter YouTube Video Title: ").strip()

        # Description prompting
        confirm_desc = input(f"Use description '{description}'? [Y/n]: ").strip().lower()
        if confirm_desc in ('n', 'no'):
            print("Enter Description (press Enter on an empty line when finished):")
            desc_lines = []
            while True:
                line = input()
                if not line:
                    break
                desc_lines.append(line)
            description = "\n".join(desc_lines)

        # Privacy prompting
        confirm_priv = input(f"Use privacy status '{privacy}'? [Y/n]: ").strip().lower()
        if confirm_priv in ('n', 'no'):
            privacy = input("Enter privacy status (public/private/unlisted): ").strip().lower()
            while privacy not in ("public", "private", "unlisted"):
                privacy = input("Invalid option. Enter privacy status (public/private/unlisted): ").strip().lower()
    else:
        if not title:
            logger.error("Title is required for YouTube upload in non-interactive mode.")
            sys.exit(1)

    upload_video(
        video_path=args.video,
        title=title,
        description=description,
        category_id=args.category,
        privacy_status=privacy,
        tags=args.tags
    )

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
