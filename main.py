import os
import re
from pathlib import Path
from typing import List, Optional

import typer
from rich import print
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter
from youtube_transcript_api.proxies import WebshareProxyConfig
import yt_dlp

app = typer.Typer(help="Download YouTube video transcripts and videos")
console = Console()

def sanitize_filename(filename: str) -> str:
    """Remove invalid characters from filename"""
    invalid_chars = r'[<>:"/\\|?*]'
    sanitized = re.sub(invalid_chars, '_', filename)
    sanitized = sanitized.strip('. ')
    return sanitized[:200] if len(sanitized) > 200 else sanitized

def extract_video_id(url: str) -> str:
    """Extract video ID from YouTube URL"""
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'(?:embed\/)([0-9A-Za-z_-]{11})',
        r'(?:v\/)([0-9A-Za-z_-]{11})',
        r'^([0-9A-Za-z_-]{11})$'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    raise ValueError(f"Could not extract video ID from URL: {url}")

def get_video_info(video_id: str) -> dict:
    """Get video information using yt-dlp"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            return {
                'title': info.get('title', f'video_{video_id}'),
                'id': video_id
            }
        except Exception as e:
            console.print(f"[red]Error getting video info for {video_id}: {e}[/red]")
            return {'title': f'video_{video_id}', 'id': video_id}

def get_playlist_info(playlist_url: str) -> dict:
    """Get playlist information and video IDs"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(playlist_url, download=False)
            return {
                'title': info.get('title', 'Unknown Playlist'),
                'entries': [entry['id'] for entry in info.get('entries', []) if entry]
            }
        except Exception as e:
            console.print(f"[red]Error getting playlist info: {e}[/red]")
            return {'title': 'Unknown Playlist', 'entries': []}

def download_transcript(video_id: str, download_path: Path, proxy_username: Optional[str] = None, proxy_password: Optional[str] = None) -> bool:
    """Download transcript for a single video"""
    try:
        video_info = get_video_info(video_id)
        title = sanitize_filename(video_info['title'])
        
        if proxy_username and proxy_password:
            proxy_config = WebshareProxyConfig(
                proxy_username=proxy_username,
                proxy_password=proxy_password,
            )
            ytt_api = YouTubeTranscriptApi(proxy_config=proxy_config)
        else:
            ytt_api = YouTubeTranscriptApi()
        
        transcript = ytt_api.fetch(video_id)
        formatter = TextFormatter()
        text_formatted = formatter.format_transcript(transcript)
        
        filename = f"{title} transcript.txt"
        file_path = download_path / filename
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(text_formatted)
        
        console.print(f"[green]✓[/green] Transcript saved: {filename}")
        return True
        
    except Exception as e:
        console.print(f"[red]✗[/red] Failed to download transcript for {video_id}: {e}")
        return False

def download_video(video_id: str, download_path: Path) -> bool:
    """Download video using yt-dlp"""
    try:
        video_info = get_video_info(video_id)
        title = sanitize_filename(video_info['title'])
        
        ydl_opts = {
            'outtmpl': str(download_path / f"{title}.%(ext)s"),
            'format': 'best[height<=720]',
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        
        console.print(f"[green]✓[/green] Video downloaded: {title}")
        return True
        
    except Exception as e:
        console.print(f"[red]✗[/red] Failed to download video for {video_id}: {e}")
        return False

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
):
    """Download transcripts (and optionally videos) from YouTube URLs"""
    
    if (username is None) != (password is None):
        console.print("[red]Error: Both -username and -password must be provided together for proxy support[/red]")
        raise typer.Exit(1)
    
    if location:
        download_path = Path(location).expanduser().resolve()
    else:
        download_path = Path.home() / "Downloads"
    
    download_path.mkdir(parents=True, exist_ok=True)
    
    console.print(f"[blue]Download location:[/blue] {download_path}")
    
    if username and password:
        console.print(f"[blue]Using Webshare proxy with username:[/blue] {username}")
    
    total_success = 0
    total_failed = 0
    
    for url in urls:
        console.print(f"\n[blue]Processing:[/blue] {url}")
        
        try:
            if 'playlist' in url or 'list=' in url:
                playlist_info = get_playlist_info(url)
                playlist_title = sanitize_filename(playlist_info['title'])
                playlist_path = download_path / playlist_title
                playlist_path.mkdir(parents=True, exist_ok=True)
                
                console.print(f"[yellow]Playlist detected:[/yellow] {playlist_title}")
                console.print(f"[yellow]Found {len(playlist_info['entries'])} videos[/yellow]")
                
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console
                ) as progress:
                    task = progress.add_task("Processing playlist...", total=len(playlist_info['entries']))
                    
                    for video_id in playlist_info['entries']:
                        if download_transcript(video_id, playlist_path, username, password):
                            total_success += 1
                        else:
                            total_failed += 1
                        
                        if download_video_flag:
                            if download_video(video_id, playlist_path):
                                pass
                            else:
                                console.print(f"[yellow]Note: Video download failed for {video_id}[/yellow]")
                        
                        progress.advance(task)
            
            else:
                video_id = extract_video_id(url)
                
                if download_transcript(video_id, download_path, username, password):
                    total_success += 1
                else:
                    total_failed += 1
                
                if download_video_flag:
                    if download_video(video_id, download_path):
                        pass
                    else:
                        console.print(f"[yellow]Note: Video download failed for {video_id}[/yellow]")
        
        except Exception as e:
            console.print(f"[red]Error processing {url}: {e}[/red]")
            total_failed += 1
    
    console.print(f"\n[bold]Summary:[/bold]")
    console.print(f"[green]✓ Successful: {total_success}[/green]")
    console.print(f"[red]✗ Failed: {total_failed}[/red]")

if __name__ == "__main__":
    app()