"""
ComfyUI-Downloader Extension
Adds a Downloader button to ComfyUI interface with modal UI
"""

import os
import logging
import asyncio
import contextlib
import hashlib
import shutil
import urllib.parse
import aiohttp
import folder_paths
from aiohttp import web
from server import PromptServer

# Track active downloads
active_downloads = {}
# Download control (for pause/resume)
download_control = {}
# Download queue management
download_queue = []
current_download_task = None  # Only one download at a time

# Polling interval for wget-backed progress updates
PROGRESS_UPDATE_INTERVAL = 0.25

# Suffix for in-progress downloads so ComfyUI never lists a partial file as a model
PARTIAL_SUFFIX = ".part"

API_PREFIX = "35b631e00fa2dbc173ee4a5f899cba8f"

# Save the original function before wrapping
original_get_filename_list = folder_paths.get_filename_list

# Wrapper for folder_paths.get_filename_list
def get_filename_list_wrapper(folder_name):
    """Wrapper for folder_paths.get_filename_list to get list of files in a folder"""
    try:
        # print("get_filename_list wrapper called for folder:", folder_name)
        result = original_get_filename_list(folder_name)
        # Prepend folder path entry for download directory
        mapped_folder = folder_paths.map_legacy(folder_name)
        if mapped_folder in folder_paths.folder_names_and_paths:
            paths, _ = folder_paths.folder_names_and_paths[mapped_folder]
            if paths and any("/models/" in path for path in paths):  # Check if paths list is not empty and contains /models/
                folder_entry = "__folder__path__" + folder_name
                if not result:
                    result = [folder_entry]
                else:
                    result = [folder_entry] + result
        return result
    except Exception as e:
        logging.error(f"[ComfyUI-Downloader] Error getting file list for {folder_name}: {e}")
        return []

folder_paths.get_filename_list = get_filename_list_wrapper


@PromptServer.instance.routes.post(f"/{API_PREFIX}/server_download/start")
async def start_download(request):
    """Start downloading a model file to the server"""
    try:
        json_data = await request.json()
        url = json_data.get("url")
        save_path = json_data.get("save_path")  # e.g., "checkpoints" or "loras"
        filename = json_data.get("filename")    # e.g., "model.safetensors" or "subfolder/model.safetensors"
        override = json_data.get("override", False)  # Allow file override if True

        if not url or not save_path or not filename:
            return web.json_response(
                {"error": "Missing required parameters: url, save_path, filename"},
                status=400
            )

        # Security: only allow http(s) URLs so the value can never be parsed as a wget option
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            return web.json_response(
                {"error": "Invalid url: only http:// and https:// URLs are supported"},
                status=400
            )

        # Security: Normalize the path first - convert backslashes to forward slashes
        # This allows Windows-style paths (subfolder\file.ext) while maintaining security
        safe_filename = os.path.normpath(filename).replace("\\", "/")
        
        # Validate filename to prevent path traversal attacks
        if ".." in safe_filename or safe_filename.startswith("/") or safe_filename.startswith("~"):
            return web.json_response(
                {"error": "Invalid filename: path traversal patterns detected"},
                status=400
            )

        # Ensure it doesn't try to escape the directory
        if safe_filename.startswith("../") or "/../" in safe_filename:
            return web.json_response(
                {"error": "Invalid filename: path traversal detected"},
                status=400
            )

        # Get the first path for this folder type from folder_paths
        mapped_folder = folder_paths.map_legacy(save_path)
        if mapped_folder not in folder_paths.folder_names_and_paths:
            return web.json_response(
                {"error": f"Invalid save_path: {save_path} not found in folder_paths"},
                status=400
            )

        paths, _ = folder_paths.folder_names_and_paths[mapped_folder]
        if not paths:
            return web.json_response(
                {"error": f"No valid paths configured for {save_path}"},
                status=400
            )

        # Filter paths to only include those containing /models/
        model_paths = [path for path in paths if "/models/" in path]
        if not model_paths:
            return web.json_response(
                {"error": f"No valid model paths (containing /models/) configured for {save_path}"},
                status=400
            )

        # Use the first path from the configured paths
        output_dir = model_paths[0]
        output_path = os.path.join(output_dir, safe_filename)

        # Final security check: ensure the resolved path is within the configured directory
        output_path = os.path.abspath(output_path)
        output_dir = os.path.abspath(output_dir)
        if not output_path.startswith(output_dir + os.sep):
            return web.json_response(
                {"error": "Security error: attempted directory escape"},
                status=400
            )

        # Use SHA256 hash for unique, URL-safe download_id
        download_id = hashlib.sha256(f"{save_path}/{safe_filename}".encode()).hexdigest()

        # Don't queue the same target twice while it is still pending
        existing = active_downloads.get(download_id)
        if existing and existing.get("status") in ("queued", "downloading"):
            return web.json_response({
                "success": True,
                "download_id": download_id,
                "message": f"Download already {existing['status']}"
            })

        # Check if file already exists
        if os.path.exists(output_path):
            if not override:
                # Request confirmation from user
                return web.json_response({
                    "confirm_override": True,
                    "message": f"File already exists: {safe_filename}",
                    "path": output_path
                })
            else:
                # User confirmed override, remove existing file
                logging.info(f"[ComfyUI-Downloader] Overriding existing file: {output_path}")
                try:
                    os.remove(output_path)
                except Exception as e:
                    return web.json_response(
                        {"error": f"Failed to remove existing file: {str(e)}"},
                        status=500
                    )

        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Mark as queued
        active_downloads[download_id] = {
            "url": url,
            "filename": safe_filename,
            "save_path": save_path,
            "output_path": output_path,
            "progress": 0,
            "status": "queued",
            "priority": None
        }

        # Add to queue
        download_queue.append({
            "download_id": download_id,
            "url": url,
            "output_path": output_path
        })

        # Process queue (will start download if slot available)
        asyncio.create_task(process_download_queue())

        return web.json_response({
            "success": True,
            "download_id": download_id,
            "message": "Download queued"
        })

    except Exception as e:
        logging.error(f"[ComfyUI-Downloader] Error starting download: {e}")
        return web.json_response(
            {"error": str(e)},
            status=500
        )


async def process_download_queue():
    """Process the download queue - one download at a time"""
    global download_queue, current_download_task

    # Check if already downloading
    if current_download_task is not None and not current_download_task.done():
        logging.info("[ComfyUI-Downloader] Download already in progress, waiting...")
        return  # Already downloading

    if len(download_queue) == 0:
        logging.info("[ComfyUI-Downloader] Queue is empty")
        return  # Nothing to process

    # Get next download from queue
    download_item = download_queue.pop(0)
    download_id = download_item["download_id"]
    url = download_item["url"]
    output_path = download_item["output_path"]

    # Set status to downloading
    active_downloads[download_id]["status"] = "downloading"
    active_downloads[download_id]["progress"] = 0
    active_downloads[download_id]["downloaded"] = 0

    logging.info(f"[ComfyUI-Downloader] Starting wget download {download_id}")

    # Notify frontend that download is starting
    await PromptServer.instance.send("server_download_progress", {
        "download_id": download_id,
        "progress": 0,
        "downloaded": 0,
        "total": 0
    })

    # Start download task
    current_download_task = asyncio.create_task(download_file(url, output_path, download_id))

    # Add completion callback to process next in queue
    current_download_task.add_done_callback(lambda t: on_download_complete(download_id))


def on_download_complete(download_id):
    """Called when a download completes - processes next in queue"""
    global current_download_task

    current_download_task = None
    logging.info(f"[ComfyUI-Downloader] Download completed: {download_id}, processing next in queue...")

    # Process next in queue
    asyncio.create_task(process_download_queue())


async def get_remote_file_size(url):
    """Best-effort remote file size lookup for progress percentages."""
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.head(url, allow_redirects=True) as response:
            return int(response.headers.get("Content-Length", 0))


async def report_wget_progress(download_id, temp_path):
    """Poll the partial file size while wget writes to disk."""
    download_state = active_downloads.get(download_id)
    if download_state is None:
        return

    total_size = download_state.get("total", 0)

    while True:
        control = download_control.get(download_id)
        if control is None:
            return

        process = control.get("process")
        if process is not None and process.returncode is not None:
            break

        if download_id not in active_downloads:
            return

        downloaded_size = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
        active_downloads[download_id]["downloaded"] = downloaded_size

        progress = 0
        if total_size > 0:
            progress = round(min(downloaded_size / total_size, 1) * 100, 2)
            active_downloads[download_id]["progress"] = progress

        await PromptServer.instance.send("server_download_progress", {
            "download_id": download_id,
            "progress": progress,
            "downloaded": downloaded_size,
            "total": total_size
        })

        await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)


async def download_file(url, output_path, download_id):
    """Download file with wget into a .part file, then move it into place on success."""
    temp_path = output_path + PARTIAL_SUFFIX
    progress_task = None
    completed = False

    try:
        wget_path = shutil.which("wget")
        if wget_path is None:
            raise RuntimeError("wget not found in PATH. Please install wget on the ComfyUI server.")

        logging.info(f"[ComfyUI-Downloader] Download {download_id} using wget")

        total_size = 0
        try:
            total_size = await get_remote_file_size(url)
        except Exception as e:
            logging.warning(f"[ComfyUI-Downloader] Could not determine file size for {download_id}: {e}")

        if download_id not in active_downloads:
            return

        active_downloads[download_id]["total"] = total_size
        active_downloads[download_id]["downloaded"] = 0

        download_control[download_id] = {
            "cancelled": False,
            "process": None,
        }

        # Always start from a clean partial file instead of resuming stale content.
        if os.path.exists(temp_path):
            os.remove(temp_path)

        process = await asyncio.create_subprocess_exec(
            wget_path,
            "--output-document",
            temp_path,
            "--",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        download_control[download_id]["process"] = process

        progress_task = asyncio.create_task(report_wget_progress(download_id, temp_path))
        stdout, stderr = await process.communicate()

        if progress_task is not None:
            await progress_task

        if download_control.get(download_id, {}).get("cancelled"):
            return

        if process.returncode != 0:
            error_output = (stderr or stdout or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(error_output or f"wget exited with code {process.returncode}")

        os.replace(temp_path, output_path)
        completed = True

        final_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        if download_id in active_downloads:
            active_downloads[download_id]["downloaded"] = final_size
            active_downloads[download_id]["total"] = max(total_size, final_size)
            active_downloads[download_id]["status"] = "completed"
            active_downloads[download_id]["progress"] = 100

        await PromptServer.instance.send("server_download_complete", {
            "download_id": download_id,
            "path": output_path,
            "size": final_size
        })

        logging.info(f"[ComfyUI-Downloader] Successfully downloaded {download_id} to {output_path}")

    except Exception as e:
        logging.error(f"[ComfyUI-Downloader] Error downloading {download_id}: {e}")
        if download_id in active_downloads:
            active_downloads[download_id]["status"] = "error"
            active_downloads[download_id]["error"] = str(e)

        await PromptServer.instance.send("server_download_error", {
            "download_id": download_id,
            "error": str(e)
        })
    finally:
        if progress_task is not None and not progress_task.done():
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task

        # Never leave a partial file behind after an error or cancellation
        if not completed and os.path.exists(temp_path):
            with contextlib.suppress(OSError):
                os.remove(temp_path)

        if download_id in download_control:
            del download_control[download_id]


@PromptServer.instance.routes.get(f"/{API_PREFIX}/server_download/status")
async def get_download_status(request):
    """Get status of all downloads"""
    return web.json_response(active_downloads)


@PromptServer.instance.routes.get(f"/{API_PREFIX}/server_download/status/{{download_id:.*}}")
async def get_single_download_status(request):
    """Get status of a specific download"""
    download_id = request.match_info.get("download_id", "")

    if download_id in active_downloads:
        return web.json_response(active_downloads[download_id])
    else:
        return web.json_response(
            {"error": "Download not found"},
            status=404
        )


@PromptServer.instance.routes.post(f"/{API_PREFIX}/server_download/cancel")
async def cancel_download(request):
    """Cancel an active download"""
    global download_queue, current_download_task

    try:
        json_data = await request.json()
        download_id = json_data.get("download_id")

        if not download_id:
            return web.json_response(
                {"error": "Missing download_id"},
                status=400
            )

        # Check if download is queued (not started yet)
        was_queued = any(d["download_id"] == download_id for d in download_queue)
        download_queue[:] = [d for d in download_queue if d["download_id"] != download_id]

        # Check if download is active
        if download_id in download_control:
            download_control[download_id]["cancelled"] = True
            if download_id in active_downloads:
                active_downloads[download_id]["status"] = "cancelled"
            process = download_control[download_id].get("process")
            if process is not None and process.returncode is None:
                process.terminate()
                with contextlib.suppress(ProcessLookupError, asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2)

        # Remove from active downloads
        if was_queued and download_id in active_downloads:
            del active_downloads[download_id]

        # Cleanup control if still present
        if download_id in download_control:
            del download_control[download_id]

        await PromptServer.instance.send("server_download_cancelled", {
            "download_id": download_id
        })

        logging.info(f"[ComfyUI-Downloader] Cancelled download: {download_id} (was_queued: {was_queued})")

        return web.json_response({"success": True, "message": "Download cancelled"})

    except Exception as e:
        logging.error(f"[ComfyUI-Downloader] Error cancelling download: {e}")
        return web.json_response({"error": str(e)}, status=500)


@PromptServer.instance.routes.get(f"/{API_PREFIX}/supported_extensions")
async def get_supported_extensions(request):
    """Get supported model file extensions from folder_paths"""
    try:
        extensions = list(folder_paths.supported_pt_extensions)
        extensions.append(".onnx")
        extensions.append(".gguf")        
        extensions.append(".sft")        
        return web.json_response({
            "success": True,
            "extensions": extensions
        })
    except Exception as e:
        logging.error(f"[ComfyUI-Downloader] Error getting supported extensions: {e}")
        return web.json_response(
            {"error": str(e)},
            status=500
        )


@PromptServer.instance.routes.get(f"/{API_PREFIX}/folder_names")
async def get_folder_names(request):
    """Get available folder names from folder_paths"""
    try:
        # Only return folders that have valid paths (non-empty paths list)
        folder_names = []
        for folder_name, (paths, _) in folder_paths.folder_names_and_paths.items():
            if paths and any("/models/" in path for path in paths):  # Check if paths list is not empty and contains /models/
                folder_names.append(folder_name)
        
        return web.json_response({
            "success": True,
            "folders": sorted(folder_names)
        })
    except Exception as e:
        logging.error(f"[ComfyUI-Downloader] Error getting folder names: {e}")
        return web.json_response(
            {"error": str(e)},
            status=500
        )


# Add route to serve CSS
@PromptServer.instance.routes.get(f"/{API_PREFIX}/extensions/ComfyUI-Downloader/css/downloader.css")
async def get_css(request):
    css_path = os.path.join(os.path.dirname(__file__), "web", "css", "downloader.css")
    with open(css_path, "r", encoding="utf-8") as f:
        css_content = f.read()
    return web.Response(text=css_content, content_type="text/css")


# Set the web directory for frontend files
WEB_DIRECTORY = "./web"

# Required by ComfyUI - this extension provides server API routes and web UI, not custom nodes
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

__all__ = ["WEB_DIRECTORY"]

print("\033[92m[ComfyUI-Downloader]\033[0m Extension loaded successfully with download API")
