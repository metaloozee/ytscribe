import os
import re
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from urllib.parse import urlparse
from urllib.error import URLError

import typer
import keyring
from rich import print
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TaskID
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter
from youtube_transcript_api.proxies import WebshareProxyConfig
import yt_dlp
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Application setup
app = typer.Typer(help="Download YouTube video transcripts and videos")
console = Console()

# Configuration
CONFIG_DIR = Path.home() / ".ytscribe"
CONFIG_FILE = CONFIG_DIR / "config.json"
KEYRING_SERVICE = "ytscribe"
KEYRING_USERNAME_KEY = "proxy_username"

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(CONFIG_DIR / "ytscribe.log") if CONFIG_DIR.exists() else logging.NullHandler(),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Network timeout constants (in seconds)
NETWORK_TIMEOUT = 30
YT_DLP_TIMEOUT = 30


def load_config() -> Dict[str, Any]:
    """Load configuration from file"""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                logger.debug(f"Configuration loaded from {CONFIG_FILE}")
                return config
        except (json.JSONDecodeError, IOError) as e:
            console.print(f"[yellow]Warning: Could not load config file ({e}), using defaults[/yellow]")
            logger.warning(f"Failed to load config: {e}")
    return {}


def save_config(config: Dict[str, Any]) -> None:
    """Save configuration to file (excluding sensitive data)"""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Don't save passwords to config file anymore
        safe_config = {k: v for k, v in config.items() if k != 'proxy' or 'password' not in str(v)}
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(safe_config, f, indent=2)
        # Set restrictive permissions on config file
        os.chmod(CONFIG_FILE, 0o600)
        logger.debug(f"Configuration saved to {CONFIG_FILE}")
    except (IOError, OSError) as e:
        console.print(f"[yellow]Warning: Could not save config file ({e})[/yellow]")
        logger.error(f"Failed to save config: {e}")


def get_proxy_credentials() -> Tuple[Optional[str], Optional[str]]:
    """Get proxy credentials from environment variables or keyring"""
    # First, try environment variables
    username = os.getenv('YTSCRIBE_PROXY_USERNAME')
    password = os.getenv('YTSCRIBE_PROXY_PASSWORD')

    if username and password:
        logger.debug("Using proxy credentials from environment variables")
        return username, password

    # Fall back to keyring
    try:
        username = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME_KEY)
        if username:
            password = keyring.get_password(KEYRING_SERVICE, username)
            if password:
                logger.debug("Using proxy credentials from keyring")
                return username, password
    except Exception as e:
        logger.warning(f"Failed to retrieve credentials from keyring: {e}")

    return None, None


def save_proxy_credentials(username: str, password: str) -> None:
    """Save proxy credentials securely to keyring"""
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME_KEY, username)
        keyring.set_password(KEYRING_SERVICE, username, password)
        logger.info("Proxy credentials saved securely to keyring")
    except Exception as e:
        console.print(f"[yellow]Warning: Could not save credentials to keyring ({e})[/yellow]")
        logger.error(f"Failed to save credentials: {e}")


def validate_url(url: str) -> bool:
    """Validate if URL is a valid YouTube URL"""
    youtube_domains = ['youtube.com', 'youtu.be', 'm.youtube.com', 'www.youtube.com']
    try:
        parsed = urlparse(url)
        return any(domain in parsed.netloc for domain in youtube_domains)
    except (ValueError, AttributeError):
        return False


def validate_video_id(video_id: str) -> bool:
    """Validate if string is a valid YouTube video ID"""
    return bool(re.match(r'^[0-9A-Za-z_-]{11}$', video_id))


def display_summary_table(successful: List[str], failed: List[str]) -> None:
    """Display a formatted summary table"""
    table = Table(title="Download Summary")
    table.add_column("Status", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Items", style="dim")

    if successful:
        table.add_row("✓ Successful", str(len(successful)), f"{len(successful)} items downloaded")
    if failed:
        table.add_row("✗ Failed", str(len(failed)), f"{len(failed)} items failed")

    console.print(table)


def display_playlist_preview_table(playlist_videos: List[Dict[str, Any]]) -> None:
    """Display a formatted table with playlist video information"""
    table = Table(title="Playlist Videos Preview", show_lines=True)
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Title", style="bold blue", min_width=30)
    table.add_column("Duration", justify="center", width=10)
    table.add_column("Channel", style="green", width=20)
    table.add_column("View Count", justify="right", width=12)
    table.add_column("Upload Date", justify="center", width=12)

    for idx, video in enumerate(playlist_videos, 1):
        duration = video.get('duration_string', 'N/A')
        view_count = video.get('view_count')
        view_count_str = f"{view_count:,}" if view_count else "N/A"
        upload_date = video.get('upload_date', 'N/A')
        if upload_date != 'N/A' and len(upload_date) == 8:
            upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"

        table.add_row(
            str(idx),
            video.get('title', 'Unknown Title')[:50] + ("..." if len(video.get('title', '')) > 50 else ""),
            duration,
            video.get('uploader', 'Unknown')[:18] + ("..." if len(video.get('uploader', '')) > 18 else ""),
            view_count_str,
            upload_date
        )

    console.print(table)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((URLError, ConnectionError, TimeoutError))
)
def get_detailed_playlist_info(playlist_url: str) -> Dict[str, Any]:
    """Get detailed playlist information including video metadata with retry logic"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'playlist_items': '1:50',
        'socket_timeout': YT_DLP_TIMEOUT,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(playlist_url, download=False)
            if info is not None:
                return {
                    'title': info.get('title', 'Unknown Playlist'),
                    'entries': info.get('entries', []),
                    'playlist_count': info.get('playlist_count', 0)
                }
            else:
                return {'title': 'Unknown Playlist', 'entries': [], 'playlist_count': 0}
        except Exception as e:
            console.print(f"[red]Error getting detailed playlist info: {e}[/red]")
            logger.error(f"Failed to get playlist info for {playlist_url}: {e}")
            return {'title': 'Unknown Playlist', 'entries': [], 'playlist_count': 0}


def download_single_item(args: Tuple) -> Tuple[bool, str, str]:
    """Download a single video transcript and/or video - thread-safe wrapper"""
    video_id, download_path, proxy_username, proxy_password, languages, download_video_flag = args

    results = []

    transcript_success, transcript_result = download_transcript(
        video_id, download_path, proxy_username, proxy_password, languages
    )
    results.append(('transcript', transcript_success, transcript_result))

    if download_video_flag:
        video_success, video_result = download_video(video_id, download_path)
        results.append(('video', video_success, video_result))

    all_success = all(result[1] for result in results)
    combined_result = " | ".join([f"{r[0]}: {r[2]}" for r in results])

    return all_success, combined_result, video_id


def sanitize_filename(filename: str) -> str:
    """Remove invalid characters from filename"""
    invalid_chars = r'[<>:"/\\|?*]'
    sanitized = re.sub(invalid_chars, '_', filename)
    sanitized = sanitized.strip('. ')

    if not sanitized:
        sanitized = "untitled"

    return sanitized[:200] if len(sanitized) > 200 else sanitized


def extract_video_id(url: str) -> str:
    """Extract video ID from YouTube URL with improved error handling"""
    if validate_video_id(url):
        return url

    if not validate_url(url):
        raise ValueError(f"Invalid YouTube URL: {url}\nSupported formats:\n"
                        "- https://youtube.com/watch?v=VIDEO_ID\n"
                        "- https://youtu.be/VIDEO_ID\n"
                        "- VIDEO_ID (11 characters)")

    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'(?:embed\/)([0-9A-Za-z_-]{11})',
        r'(?:v\/)([0-9A-Za-z_-]{11})',
        r'^([0-9A-Za-z_-]{11})$'
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            if validate_video_id(video_id):
                return video_id

    raise ValueError(f"Could not extract valid video ID from URL: {url}\n"
                    "Make sure the URL contains a valid 11-character video ID")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((URLError, ConnectionError, TimeoutError))
)
def get_video_info(video_id: str) -> Dict[str, str]:
    """Get video information using yt-dlp with retry logic"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'socket_timeout': YT_DLP_TIMEOUT,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            if info is not None:
                return {
                    'title': info.get('title', f'video_{video_id}'),
                    'id': video_id
                }
            else:
                return {'title': f'video_{video_id}', 'id': video_id}
        except Exception as e:
            console.print(f"[red]Error getting video info for {video_id}: {e}[/red]")
            logger.error(f"Failed to get video info for {video_id}: {e}")
            return {'title': f'video_{video_id}', 'id': video_id}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((URLError, ConnectionError, TimeoutError))
)
def get_playlist_info(playlist_url: str) -> Dict[str, Any]:
    """Get playlist information and video IDs with retry logic"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'socket_timeout': YT_DLP_TIMEOUT,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(playlist_url, download=False)
            if info is not None:
                return {
                    'title': info.get('title', 'Unknown Playlist'),
                    'entries': [entry['id'] for entry in info.get('entries', []) if entry and 'id' in entry]
                }
            else:
                return {'title': 'Unknown Playlist', 'entries': []}
        except Exception as e:
            console.print(f"[red]Error getting playlist info: {e}[/red]")
            logger.error(f"Failed to get playlist info for {playlist_url}: {e}")
            return {'title': 'Unknown Playlist', 'entries': []}


def download_transcript(
    video_id: str,
    download_path: Path,
    proxy_username: Optional[str] = None,
    proxy_password: Optional[str] = None,
    languages: Optional[List[str]] = None,
    progress_task: Optional[TaskID] = None,
    progress: Optional[Progress] = None
) -> Tuple[bool, str]:
    """Download transcript for a single video with improved error handling and progress tracking"""
    try:
        if progress and progress_task:
            progress.update(progress_task, description=f"Getting video info for {video_id[:8]}...")

        video_info = get_video_info(video_id)
        title = sanitize_filename(video_info['title'])

        if progress and progress_task:
            progress.update(progress_task, description=f"Fetching transcript for '{title[:30]}...'")

        if proxy_username and proxy_password:
            try:
                proxy_config = WebshareProxyConfig(
                    proxy_username=proxy_username,
                    proxy_password=proxy_password,
                )
                ytt_api = YouTubeTranscriptApi(proxy_config=proxy_config)
            except Exception as proxy_error:
                console.print(f"[yellow]Warning: Proxy configuration failed ({proxy_error}), using direct connection[/yellow]")
                logger.warning(f"Proxy configuration failed: {proxy_error}")
                ytt_api = YouTubeTranscriptApi()
        else:
            ytt_api = YouTubeTranscriptApi()

        try:
            if languages:
                transcript = ytt_api.fetch(video_id, languages=languages)
            else:
                transcript = ytt_api.fetch(video_id)
        except Exception as transcript_error:
            error_msg = str(transcript_error)
            if "No transcripts were found" in error_msg:
                return False, f"No transcripts available for '{title}'"
            elif "Could not retrieve a transcript" in error_msg:
                return False, f"Transcript unavailable for '{title}' (may be disabled or private)"
            elif "subtitles are disabled" in error_msg:
                return False, f"Subtitles are disabled for '{title}'"
            else:
                logger.error(f"Transcript error for {video_id}: {error_msg}")
                return False, f"Transcript error for '{title}': {error_msg}"

        formatter = TextFormatter()
        text_formatted = formatter.format_transcript(transcript)

        if progress and progress_task:
            progress.update(progress_task, description=f"Saving transcript for '{title[:30]}...'")

        language_info = ""
        if hasattr(transcript, 'language_code') and hasattr(transcript, 'language'):
            language_info = f" ({transcript.language_code}: {transcript.language})"

        filename = f"{title} transcript.txt"
        file_path = download_path / filename

        # Use atomic file writing to prevent race conditions
        try:
            fd = os.open(str(file_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(text_formatted)
            console.print(f"[green]✓[/green] Transcript saved: {filename}{language_info}")
            logger.info(f"Transcript saved: {filename}")
            return True, title
        except FileExistsError:
            console.print(f"[yellow]⚠[/yellow] File already exists: {filename}")
            logger.debug(f"File already exists: {filename}")
            return True, f"Already exists: {title}"

    except Exception as e:
        error_msg = f"Unexpected error for {video_id}: {str(e)}"
        console.print(f"[red]✗[/red] {error_msg}")
        logger.error(error_msg)
        return False, error_msg


def download_video(
    video_id: str,
    download_path: Path,
    progress_task: Optional[TaskID] = None,
    progress: Optional[Progress] = None
) -> Tuple[bool, str]:
    """Download video using yt-dlp with improved error handling"""
    try:
        if progress and progress_task:
            progress.update(progress_task, description=f"Getting video info for {video_id[:8]}...")

        video_info = get_video_info(video_id)
        title = sanitize_filename(video_info['title'])

        if progress and progress_task:
            progress.update(progress_task, description=f"Downloading video '{title[:30]}...'")

        existing_files = list(download_path.glob(f"{title}.*"))
        if existing_files:
            console.print(f"[yellow]⚠[/yellow] Video file already exists: {existing_files[0].name}")
            logger.debug(f"Video file already exists: {existing_files[0].name}")
            return True, f"Already exists: {title}"

        ydl_opts = {
            'outtmpl': str(download_path / f"{title}.%(ext)s"),
            'format': 'best[height<=720]',
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': YT_DLP_TIMEOUT,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

        console.print(f"[green]✓[/green] Video downloaded: {title}")
        logger.info(f"Video downloaded: {title}")
        return True, title

    except Exception as e:
        error_msg = f"Video download failed for '{video_info.get('title', video_id)}': {str(e)}"
        console.print(f"[red]✗[/red] {error_msg}")
        logger.error(error_msg)
        return False, error_msg


def _validate_urls(urls: List[str]) -> Tuple[List[str], List[str]]:
    """Validate a list of URLs and return valid and invalid lists"""
    valid_urls = []
    invalid_urls = []

    for url in urls:
        try:
            if validate_url(url) or validate_video_id(url):
                valid_urls.append(url)
            else:
                invalid_urls.append(url)
        except (ValueError, AttributeError):
            invalid_urls.append(url)

    return valid_urls, invalid_urls


def _load_credentials(config: Dict[str, Any], username: Optional[str], password: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Load proxy credentials from various sources"""
    # Priority: CLI args > Environment > Keyring > Config file (legacy)
    if username and password:
        return username, password

    # Try environment variables or keyring
    env_user, env_pass = get_proxy_credentials()
    if env_user and env_pass:
        return env_user, env_pass

    # Fall back to config file (legacy, for backwards compatibility)
    if 'proxy' in config:
        return config['proxy'].get('username'), config['proxy'].get('password')

    return None, None


def _get_download_path(location: Optional[str], config: Dict[str, Any]) -> Path:
    """Determine the download path from location or config"""
    if location:
        return Path(location).expanduser().resolve()
    elif 'default_location' in config:
        return Path(config['default_location']).expanduser().resolve()
    else:
        return Path.home() / "Downloads"


def _separate_playlists_and_videos(urls: List[str]) -> Tuple[List[str], List[str]]:
    """Separate URLs into playlists and individual videos"""
    playlist_urls = []
    individual_video_urls = []

    for url in urls:
        if 'playlist' in url or 'list=' in url:
            playlist_urls.append(url)
        else:
            individual_video_urls.append(url)

    return playlist_urls, individual_video_urls


def _execute_concurrent_downloads(
    download_args: List[Tuple],
    max_concurrent: int,
    verbose: bool,
    description: str = "Processing items concurrently..."
) -> Tuple[List[str], List[str]]:
    """Execute concurrent downloads using ThreadPoolExecutor"""
    successful_items = []
    failed_items = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console
    ) as progress:
        task = progress.add_task(description, total=len(download_args))

        with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
            future_to_args = {executor.submit(download_single_item, args): args for args in download_args}

            for future in as_completed(future_to_args):
                try:
                    success, result, video_id = future.result()
                    if success:
                        successful_items.append(result)
                    else:
                        failed_items.append(result)

                    if verbose:
                        status = "✓" if success else "✗"
                        console.print(f"[{'green' if success else 'red'}]{status}[/] {video_id}: {result}")

                except Exception as e:
                    args = future_to_args[future]
                    video_id = args[0]
                    error_msg = f"Exception processing {video_id}: {str(e)}"
                    failed_items.append(error_msg)
                    logger.error(error_msg)
                    if verbose:
                        console.print(f"[red]✗[/] {error_msg}")

                progress.advance(task)

    return successful_items, failed_items


def _process_playlists(
    playlist_urls: List[str],
    download_path: Path,
    username: Optional[str],
    password: Optional[str],
    languages: Optional[List[str]],
    download_video_flag: bool,
    max_concurrent: int,
    verbose: bool
) -> Tuple[List[str], List[str]]:
    """Process all playlists and return successful and failed items"""
    successful_items = []
    failed_items = []

    for url_idx, url in enumerate(playlist_urls):
        console.print(f"\n[blue]Processing Playlist ({url_idx + 1}/{len(playlist_urls)}):[/blue] {url}")

        try:
            playlist_info = get_playlist_info(url)
            playlist_title = sanitize_filename(playlist_info['title'])
            playlist_path = download_path / playlist_title
            playlist_path.mkdir(parents=True, exist_ok=True)

            console.print(f"[yellow]📋 Playlist:[/yellow] {playlist_title}")
            console.print(f"[yellow]Found {len(playlist_info['entries'])} videos[/yellow]")
            console.print(f"[blue]Concurrent downloads:[/blue] {max_concurrent}")

            if not playlist_info['entries']:
                console.print("[red]No videos found in playlist[/red]")
                continue

            download_args = [
                (video_id, playlist_path, username, password, languages, download_video_flag)
                for video_id in playlist_info['entries']
            ]

            success, failed = _execute_concurrent_downloads(
                download_args, max_concurrent, verbose, "Processing playlist concurrently..."
            )
            successful_items.extend(success)
            failed_items.extend(failed)

        except Exception as e:
            error_msg = f"Error processing playlist {url}: {str(e)}"
            console.print(f"[red]✗ {error_msg}[/red]")
            logger.error(error_msg)
            failed_items.append(error_msg)

    return successful_items, failed_items


def _process_individual_videos(
    individual_video_urls: List[str],
    download_path: Path,
    username: Optional[str],
    password: Optional[str],
    languages: Optional[List[str]],
    download_video_flag: bool,
    max_concurrent: int,
    verbose: bool
) -> Tuple[List[str], List[str]]:
    """Process individual videos and return successful and failed items"""
    successful_items = []
    failed_items = []

    if not individual_video_urls:
        return successful_items, failed_items

    if len(individual_video_urls) == 1:
        url = individual_video_urls[0]
        console.print(f"\n[blue]Processing Single Video:[/blue] {url}")

        try:
            video_id = extract_video_id(url)

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console
            ) as progress:
                task = progress.add_task("Processing video...", total=1)

                success, result = download_transcript(
                    video_id, download_path, username, password, languages, task, progress
                )
                if success:
                    successful_items.append(result)
                else:
                    failed_items.append(result)

                if download_video_flag:
                    vid_success, vid_result = download_video(video_id, download_path, task, progress)
                    if not vid_success and verbose:
                        console.print(f"[yellow]Note: {vid_result}[/yellow]")

                progress.advance(task)

        except Exception as e:
            error_msg = f"Error processing {url}: {str(e)}"
            console.print(f"[red]✗ {error_msg}[/red]")
            logger.error(error_msg)
            failed_items.append(error_msg)

    else:
        console.print(f"\n[blue]Processing {len(individual_video_urls)} Individual Videos Concurrently:[/blue]")
        console.print(f"[blue]Concurrent downloads:[/blue] {max_concurrent}")

        video_download_args = []
        for url in individual_video_urls:
            try:
                video_id = extract_video_id(url)
                video_download_args.append((video_id, download_path, username, password, languages, download_video_flag))
            except Exception as e:
                error_msg = f"Error extracting video ID from {url}: {str(e)}"
                console.print(f"[red]✗ {error_msg}[/red]")
                logger.error(error_msg)
                failed_items.append(error_msg)

        if video_download_args:
            success, failed = _execute_concurrent_downloads(
                video_download_args, max_concurrent, verbose, "Processing videos concurrently..."
            )
            successful_items.extend(success)
            failed_items.extend(failed)

    return successful_items, failed_items


@app.command()
def download(
    urls: List[str] = typer.Argument(..., help="YouTube video URLs, video IDs, or playlist URLs"),
    location: Optional[str] = typer.Option(
        None,
        "-l", "--location",
        help="Download location (default: ~/Downloads)"
    ),
    download_video_flag: bool = typer.Option(
        False,
        "-vid", "--video",
        help="Also download the video files"
    ),
    username: Optional[str] = typer.Option(
        None,
        "-username",
        help="Webshare proxy username (must be used with --password)"
    ),
    password: Optional[str] = typer.Option(
        None,
        "-password",
        help="Webshare proxy password (must be used with --username)"
    ),
    languages: Optional[List[str]] = typer.Option(
        None,
        "-language", "--languages",
        help="Preferred language codes for transcripts (e.g., 'en', 'es', 'fr'). Multiple languages can be specified in order of preference."
    ),
    preview: bool = typer.Option(
        False,
        "--preview",
        help="Preview what will be downloaded without actually downloading"
    ),
    max_concurrent: int = typer.Option(
        3,
        "--max-concurrent", "-c",
        help="Maximum number of concurrent downloads for playlists (default: 3)"
    ),
    verbose: bool = typer.Option(
        False,
        "-v", "--verbose",
        help="Show detailed output"
    ),
    save_config: bool = typer.Option(
        False,
        "--save-config",
        help="Save proxy settings and location to config file"
    ),
    force: bool = typer.Option(
        False,
        "-f", "--force",
        help="Skip confirmation prompts"
    ),
):
    """Download transcripts (and optionally videos) from YouTube URLs"""

    # Enable debug logging if verbose
    if verbose:
        logger.setLevel(logging.DEBUG)

    config = load_config()

    # Validate proxy credentials
    if (username is None) != (password is None):
        console.print("[red]Error: Both -username and -password must be provided together for proxy support[/red]")
        raise typer.Exit(1)

    # Load credentials from various sources
    username, password = _load_credentials(config, username, password)

    # Determine download path
    download_path = _get_download_path(location, config)

    # Validate URLs
    valid_urls, invalid_urls = _validate_urls(urls)

    if invalid_urls:
        console.print("[red]Invalid URLs detected:[/red]")
        for invalid_url in invalid_urls:
            console.print(f"  [red]✗[/red] {invalid_url}")

        if not force and not Confirm.ask("\nContinue with valid URLs only?"):
            raise typer.Exit(1)

        if not valid_urls:
            console.print("[red]No valid URLs to process[/red]")
            raise typer.Exit(1)

    # Create download directory
    try:
        download_path.mkdir(parents=True, exist_ok=True)
    except (IOError, OSError) as e:
        console.print(f"[red]Error creating download directory {download_path}: {e}[/red]")
        logger.error(f"Failed to create download directory: {e}")
        raise typer.Exit(1)

    # Display configuration
    config_panel = Panel.fit(
        f"[bold blue]Download Configuration[/bold blue]\n"
        f"📁 Location: [cyan]{download_path}[/cyan]\n"
        f"📝 Transcripts: [green]Yes[/green]\n"
        f"🎥 Videos: [{'green' if download_video_flag else 'red'}]{'Yes' if download_video_flag else 'No'}[/{'green' if download_video_flag else 'red'}]\n"
        f"🌐 Proxy: [{'green' if username and password else 'red'}]{'Yes' if username and password else 'No'}[/{'green' if username and password else 'red'}]\n"
        f"🌍 Languages: [cyan]{', '.join(languages) if languages else 'Auto-detect'}[/cyan]\n"
        f"⚡ Concurrency: [cyan]{max_concurrent} threads[/cyan]",
        border_style="blue"
    )
    console.print(config_panel)

    if verbose:
        console.print(f"[dim]Config file: {CONFIG_FILE}[/dim]")
        if username:
            console.print(f"[dim]Proxy username: {username}[/dim]")

    # Preview mode
    if preview:
        console.print("\n[yellow]🔍 PREVIEW - What will be downloaded:[/yellow]")
        preview_count = 0
        for url in valid_urls:
            try:
                if 'playlist' in url or 'list=' in url:
                    console.print(f"\n[blue]📋 Processing playlist preview...[/blue]")
                    detailed_playlist_info = get_detailed_playlist_info(url)
                    console.print(f"[blue]Playlist:[/blue] {detailed_playlist_info['title']}")

                    if detailed_playlist_info['entries']:
                        display_playlist_preview_table(detailed_playlist_info['entries'])
                        preview_count += len(detailed_playlist_info['entries'])

                        total_count = detailed_playlist_info.get('playlist_count', len(detailed_playlist_info['entries']))
                        if total_count > len(detailed_playlist_info['entries']):
                            console.print(f"[dim]Showing first {len(detailed_playlist_info['entries'])} of {total_count} videos[/dim]")
                    else:
                        console.print("[red]No videos found in playlist[/red]")
                else:
                    video_id = extract_video_id(url)
                    video_info = get_video_info(video_id)
                    console.print(f"\n[blue]🎥 Video:[/blue] {video_info['title']}")
                    preview_count += 1
            except Exception as e:
                console.print(f"[red]✗ Error previewing {url}: {e}[/red]")
                logger.error(f"Error previewing {url}: {e}")

        console.print(f"\n[bold]Total items to download: {preview_count}[/bold]")
        console.print("[yellow]Remove --preview to proceed with actual download[/yellow]")
        return

    # Confirmation
    if not force and len(valid_urls) > 1:
        if not Confirm.ask(f"\nProceed with downloading from {len(valid_urls)} URLs?"):
            console.print("[yellow]Operation cancelled[/yellow]")
            return

    # Save configuration
    if save_config:
        if username and password:
            save_proxy_credentials(username, password)
            # Don't save password to config file, only username for reference
            config['proxy'] = {'username': username}
        if location:
            config['default_location'] = str(download_path)
        save_config(config)
        console.print("[green]✓ Configuration saved (credentials stored securely in keyring)[/green]")

    # Separate playlists and individual videos
    playlist_urls, individual_video_urls = _separate_playlists_and_videos(valid_urls)

    # Process playlists
    playlist_success, playlist_failed = _process_playlists(
        playlist_urls, download_path, username, password, languages,
        download_video_flag, max_concurrent, verbose
    )

    # Process individual videos
    video_success, video_failed = _process_individual_videos(
        individual_video_urls, download_path, username, password, languages,
        download_video_flag, max_concurrent, verbose
    )

    # Combine results
    successful_items = playlist_success + video_success
    failed_items = playlist_failed + video_failed

    # Display summary
    console.print("\n" + "="*50)
    display_summary_table(successful_items, failed_items)

    if failed_items and verbose:
        console.print("\n[red]Failed items:[/red]")
        for item in failed_items[:5]:
            console.print(f"  [red]✗[/red] {item}")
        if len(failed_items) > 5:
            console.print(f"  [dim]... and {len(failed_items) - 5} more[/dim]")

    console.print(f"\n[blue]📁 Downloads saved to:[/blue] [cyan]{download_path}[/cyan]")


@app.command()
def config(
    show: bool = typer.Option(False, "--show", help="Show current configuration"),
    reset: bool = typer.Option(False, "--reset", help="Reset configuration to defaults"),
    set_location: Optional[str] = typer.Option(None, "--location", help="Set default download location"),
    set_proxy_username: Optional[str] = typer.Option(None, "--proxy-username", help="Set proxy username"),
    set_proxy_password: Optional[str] = typer.Option(None, "--proxy-password", help="Set proxy password"),
):
    """Manage ytscribe configuration"""

    if reset:
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
            console.print("[green]✓ Configuration reset to defaults[/green]")

        # Also clear keyring
        try:
            username = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME_KEY)
            if username:
                keyring.delete_password(KEYRING_SERVICE, username)
                keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME_KEY)
            console.print("[green]✓ Keyring credentials cleared[/green]")
        except Exception:
            pass
        return

    config_data = load_config()

    if set_location:
        location_path = Path(set_location).expanduser().resolve()
        try:
            location_path.mkdir(parents=True, exist_ok=True)
            config_data['default_location'] = str(location_path)
            console.print(f"[green]✓ Default location set to: {location_path}[/green]")
        except (IOError, OSError) as e:
            console.print(f"[red]Error setting location: {e}[/red]")
            logger.error(f"Failed to set location: {e}")
            return

    if set_proxy_username or set_proxy_password:
        # Get existing credentials
        existing_username, existing_password = get_proxy_credentials()

        new_username = set_proxy_username or existing_username
        new_password = set_proxy_password or existing_password

        if new_username and new_password:
            save_proxy_credentials(new_username, new_password)
            # Store username reference in config
            if 'proxy' not in config_data:
                config_data['proxy'] = {}
            config_data['proxy']['username'] = new_username
            console.print(f"[green]✓ Proxy credentials saved securely to keyring[/green]")
        else:
            console.print("[yellow]Warning: Both username and password are required for proxy configuration[/yellow]")

    if set_location or set_proxy_username or set_proxy_password:
        save_config(config_data)

    if show or not any([set_location, set_proxy_username, set_proxy_password, reset]):
        console.print("\n[bold blue]Current Configuration:[/bold blue]")

        if config_data or get_proxy_credentials()[0]:
            if 'default_location' in config_data:
                console.print(f"📁 Default location: [cyan]{config_data['default_location']}[/cyan]")

            # Check keyring for proxy credentials
            stored_username, stored_password = get_proxy_credentials()
            if stored_username:
                console.print(f"🌐 Proxy username: [cyan]{stored_username}[/cyan]")
                console.print(f"🌐 Proxy password: [cyan]{'Set (stored securely in keyring)' if stored_password else 'Not set'}[/cyan]")

            console.print(f"\n[dim]Config file: {CONFIG_FILE}[/dim]")
            console.print(f"[dim]Credentials storage: Keyring (secure) or Environment Variables[/dim]")
        else:
            console.print("[yellow]No configuration found. Using defaults.[/yellow]")
            console.print(f"📁 Default location: [cyan]{Path.home() / 'Downloads'}[/cyan]")
            console.print("🌐 Proxy: [red]Not configured[/red]")


@app.command()
def info():
    """Show information about ytscribe"""
    info_text = Text()
    info_text.append("ytscribe", style="bold blue")
    info_text.append(" - YouTube Transcript & Video Downloader\n\n")
    info_text.append("Version: 0.3.0\n\n", style="dim")
    info_text.append("Features:\n", style="bold")
    info_text.append("• Download transcripts from YouTube videos and playlists\n")
    info_text.append("• Optional video downloading with quality control\n")
    info_text.append("• Proxy support for restricted regions\n")
    info_text.append("• Multiple language transcript support\n")
    info_text.append("• Configuration persistence\n")
    info_text.append("• Preview mode with detailed playlist tables\n")
    info_text.append("• Concurrent downloads for playlists and multiple videos\n")
    info_text.append("• Interactive prompts and detailed progress tracking\n")
    info_text.append("• Secure credential storage with keyring\n")
    info_text.append("• Automatic retry with exponential backoff\n")
    info_text.append("• Environment variable support\n\n")
    info_text.append("Examples:\n", style="bold")
    info_text.append("  ytscribe download 'https://youtube.com/watch?v=VIDEO_ID'\n")
    info_text.append("  ytscribe download VIDEO_ID --video --location ~/Videos\n")
    info_text.append("  ytscribe download VIDEO_ID1 VIDEO_ID2 VIDEO_ID3 --max-concurrent 5\n")
    info_text.append("  ytscribe download PLAYLIST_URL --preview\n")
    info_text.append("  ytscribe download PLAYLIST_URL --max-concurrent 5\n")
    info_text.append("  ytscribe config --show\n\n")
    info_text.append("Environment Variables:\n", style="bold")
    info_text.append("  YTSCRIBE_PROXY_USERNAME - Proxy username\n")
    info_text.append("  YTSCRIBE_PROXY_PASSWORD - Proxy password\n")

    panel = Panel(info_text, border_style="blue", title="About ytscribe v0.3.0")
    console.print(panel)


if __name__ == "__main__":
    app()
