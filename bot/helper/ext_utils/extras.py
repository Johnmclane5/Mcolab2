import re
import aiohttp
from ...core.config_manager import Config
from logging import getLogger

LOGGER = getLogger(__name__)

def remove_redandent(filename):
    """
    Remove common username patterns from a filename while preserving the content title.

    Args:
        filename (str): The input filename

    Returns:
        str: Filename with usernames removed
    """
    filename = filename.replace("\n", "\\n")

    patterns = [
        r"^@[\w\.-]+?(?=_)",
        r"_@[A-Za-z]+_|@[A-Za-z]+_|[\[\]\s@]*@[^.\s\[\]]+[\]\[\s@]*",  
        r"^[\w\.-]+?(?=_Uploads_)",  
        r"^(?:by|from)[\s_-]+[\w\.-]+?(?=_)",  
        r"^\[[\w\.-]+?\][\s_-]*",  
        r"^\([\w\.-]+?\)[\s_-]*",  
    ]

    result = filename
    for pattern in patterns:
        match = re.search(pattern, result)
        if match:
            result = re.sub(pattern, " ", result)
            break  

    
    result = re.sub(r"^[_\s-]+|[_\s-]+$", " ", result)

    return result

async def remove_extension(caption):
    try:
        # Remove .mkv and .mp4 extensions if present
        cleaned_caption = re.sub(r'\.mkv|\.mp4|\.webm', '', caption)
        return cleaned_caption
    except Exception as e:
        LOGGER.error(e)
        return None
    
async def remove_unwanted(caption):
    try:
        # Match and keep everything up to and including the extension
        match = re.match(r'^(.*?\.(mkv|mp4|webm))', caption, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        return caption  # Return original if no match
    except Exception as e:
        LOGGER.error(e)
        return None

async def get_movie_poster(movie_name, release_year=None):
    tmdb_search_url = f'https://api.themoviedb.org/3/search/movie?api_key={Config.TMDB_API_KEY}&query={movie_name}'
    try:
        if release_year:
            tmdb_search_url += f'&year={release_year}'
        async with aiohttp.ClientSession() as session:
            async with session.get(tmdb_search_url) as search_response:
                search_data = await search_response.json()
                if search_data.get('results'):
                    results = search_data['results']
                    if results:
                        result = results[0]
                        poster_path = result.get('poster_path', None)
                        return f"https://image.tmdb.org/t/p/original{poster_path}"
        return None
    except Exception as e:
        LOGGER.error(f"Error fetching TMDb movie by name: {e}")
        return

async def get_tv_poster(tv_name, first_air_year=None):
    tmdb_search_url = f'https://api.themoviedb.org/3/search/tv?api_key={Config.TMDB_API_KEY}&query={tv_name}'
    try:
        if first_air_year:
            tmdb_search_url += f'&first_air_date_year={first_air_year}'
        async with aiohttp.ClientSession() as session:
            async with session.get(tmdb_search_url) as search_response:
                search_data = await search_response.json()
                if search_data.get('results'):
                    results = search_data['results']
                    if results:
                        result = results[0]
                        poster_path = result.get('poster_path', None)
                        return f"https://image.tmdb.org/t/p/original{poster_path}"
        return None
    except Exception as e:
        LOGGER.error(f"Error fetching TMDb TV by name: {e}")
        return

async def extract_file_info(message, channel_id=None):
    """Extract file info from a Pyrogram message."""
    caption_name = message.caption.strip() if message.caption else None
    file_info = {
        "channel_id": channel_id if channel_id is not None else message.chat.id,
        "message_id": message.id,
        "file_name": None,
        "file_size": None,
        "file_format": None,
    }
    if message.document:
        file_info["file_name"] = caption_name or message.document.file_name
        file_info["file_size"] = message.document.file_size
        file_info["file_format"] = message.document.mime_type
    elif message.video:
        file_info["file_name"] = caption_name or (message.video.file_name or "video.mp4")
        file_info["file_size"] = message.video.file_size
        file_info["file_format"] = message.video.mime_type
    elif message.audio:
        file_info["file_name"] = caption_name or (message.audio.file_name or "audio.mp3")
        file_info["file_size"] = message.audio.file_size
        file_info["file_format"] = message.audio.mime_type
    elif message.photo:
        file_info["file_name"] = caption_name or "photo.jpg"
        file_info["file_size"] = getattr(message.photo, "file_size", None)
        file_info["file_format"] = "image/jpeg"
    if file_info["file_name"]:
        file_info["file_name"] = await remove_extension(re.sub(r"[',]", "", file_info["file_name"].replace("&", "and")))
    return file_info