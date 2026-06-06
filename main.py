import os
import re
import json
import asyncio
import yt_dlp
import requests
import logging
from typing import List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from urllib.parse import quote
import uvicorn

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("NexusCloudBackend")

app = FastAPI(title="Nexus Cloud API", description="Stateless backend for Nexus Music App")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"message": "Welcome to Nexus Cloud API"}

@app.get("/health")
@app.get("/ping")
async def health_check():
    return {"status": "online", "message": "Nexus Cloud API is running"}

class StreamRequest(BaseModel):
    url: str

@app.get("/search")
async def search(query: str):
    is_url = query.startswith("http")
    search_query = query if is_url else f"ytsearch20:{query}"
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'extract_flat': 'in_playlist' if is_url else True, 
        'skip_download': True,
        'extractor_args': {
            'youtube': {
                'client': ['android', 'ios', 'mweb', 'web']
            }
        }
    }
    
    try:
        def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(search_query, download=False)
                
        info = await asyncio.to_thread(extract)
        
        results = []
        if 'entries' in info:
            for entry in info['entries']:
                if entry:
                    thumbnail_url = ""
                    if entry.get("thumbnails"):
                        thumbnail_url = entry["thumbnails"][-1]["url"]
                    elif entry.get("thumbnail"):
                        thumbnail_url = entry["thumbnail"]
                        
                    duration_secs = entry.get("duration")
                    duration = "0:00"
                    if duration_secs:
                        mins = int(duration_secs) // 60
                        secs = int(duration_secs) % 60
                        duration = f"{mins}:{secs:02d}"

                    results.append({
                        "id": entry.get("id"),
                        "title": entry.get("title"),
                        "artist": entry.get("uploader", entry.get("channel", "Unknown Artist")),
                        "thumbnail": thumbnail_url,
                        "url": entry.get("url", f"https://www.youtube.com/watch?v={entry.get('id')}"),
                        "duration": duration
                    })
        else:
            results.append({
                "id": info.get("id"),
                "title": info.get("title"),
                "artist": info.get("uploader", "Unknown Artist"),
                "thumbnail": info.get("thumbnail", ""),
                "url": info.get("original_url", query),
                "duration": "0:00"
            })
            
        return {"status": "success", "results": results}
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/stream-url")
async def get_stream_url(url: str):
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'skip_download': True,
        'youtube_include_dash_manifest': False,
        'extractor_args': {
            'youtube': {
                'client': ['android', 'ios', 'mweb', 'web']
            }
        }
    }
    try:
        def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
                
        info = await asyncio.to_thread(extract)
        stream_url = info.get("url")
        if not stream_url:
            raise Exception("Could not extract stream URL")
            
        return {"status": "success", "stream_url": stream_url, "title": info.get("title")}
    except Exception as e:
        logger.error(f"Stream extraction error for {url}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def extract_artist_track(title: str, fallback_artist: str = ""):
    # Split by common separators: hyphen, cross sign, pipe, or "by"
    separators = [r'\s-\s', r'\s×\s', r'\s\|\s', r'\sby\s']
    pattern = '|'.join(separators)
    
    parts = re.split(pattern, title, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        artist = parts[0].strip()
        track = parts[1].strip()
    else:
        parts = re.split(r'[-×|]', title, maxsplit=1)
        if len(parts) == 2:
            artist = parts[0].strip()
            track = parts[1].strip()
        else:
            artist = fallback_artist
            track = title

    artist = re.sub(r'\(.*?\)', '', artist)
    artist = re.sub(r'\[.*?\]', '', artist)
    artist = re.sub(r'[^a-zA-Z0-9\s]', '', artist)
    artist = re.sub(r'\s+', ' ', artist).strip()

    track = re.sub(r'\(.*?\)', '', track)
    track = re.sub(r'\[.*?\]', '', track)
    track = re.sub(r'official\s+(video|audio|lyrics|music\s+video|audio\s+track|music|clip)', '', track, flags=re.IGNORECASE)
    track = re.sub(r'(video|audio|lyrics|music\s+video|audio\s+track|music|clip)\s+official', '', track, flags=re.IGNORECASE)
    track = re.sub(r'\b(mv|hd|lyrics|audio|video|official|remix|slowed|reverb)\b', '', track, flags=re.IGNORECASE)
    track = re.sub(r'[^a-zA-Z0-9\s]', '', track)
    track = re.sub(r'\s+', ' ', track).strip()

    generic_artists = {
        "lyrics", "vevo", "channel", "records", "music", "hq", "uploader", "official", 
        "video", "audio", "india", "mashup", "slowed", "reverb", "remix", "audioandlyrics", 
        "lyricsforyou", "housemusichd", "saregamamusic", "primevideo", "acvkannada", "indieindia"
    }
    clean_fallback = re.sub(r'[^a-zA-Z0-9\s]', '', fallback_artist).strip()
    
    artist_norm = re.sub(r'[^a-zA-Z0-9]', '', artist).lower()
    fallback_norm = re.sub(r'[^a-zA-Z0-9]', '', clean_fallback).lower()
    
    if not artist or artist_norm in generic_artists:
        if clean_fallback and fallback_norm not in generic_artists:
            artist = clean_fallback
        else:
            artist = ""

    return artist, track

@app.get("/lyrics")
async def get_lyrics(title: str, artist: str = ""):
    clean_a, clean_t = extract_artist_track(title, artist)
    headers = {"User-Agent": "NexusMusicApp/1.0.0"}
    
    if clean_a and clean_t:
        url = f"https://lrclib.net/api/get?track_name={requests.utils.quote(clean_t)}&artist_name={requests.utils.quote(clean_a)}"
        try:
            def fetch_a(): return requests.get(url, headers=headers, timeout=5)
            response = await asyncio.to_thread(fetch_a)
            if response.status_code == 200:
                data = response.json()
                if data.get("syncedLyrics") or data.get("plainLyrics"):
                    return data
        except Exception:
            pass

    clean_full_title = re.sub(r'\(.*?\)', '', title)
    clean_full_title = re.sub(r'\[.*?\]', '', clean_full_title)
    clean_full_title = re.sub(r'official\s+(video|audio|lyrics|music\s+video|audio\s+track|music|clip)', '', clean_full_title, flags=re.IGNORECASE)
    clean_full_title = re.sub(r'[^a-zA-Z0-9\s]', ' ', clean_full_title)
    clean_full_title = re.sub(r'\s+', ' ', clean_full_title).strip()
    
    clean_fallback = re.sub(r'[^a-zA-Z0-9\s]', '', artist).strip()
    query = clean_full_title
    if clean_fallback and clean_fallback.lower() not in clean_full_title.lower():
        query = f"{clean_fallback} {clean_full_title}"
        
    url_search = f"https://lrclib.net/api/search?q={requests.utils.quote(query)}"
    try:
        def fetch_b(): return requests.get(url_search, headers=headers, timeout=5)
        response = await asyncio.to_thread(fetch_b)
        if response.status_code == 200:
            results = response.json()
            if results:
                for r in results:
                    if r.get("syncedLyrics"):
                        return r
                return results[0]
    except Exception:
        pass
        
    return {"syncedLyrics": None}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
