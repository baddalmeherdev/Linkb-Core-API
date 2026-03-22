import logging
import yt_dlp
from flask import Flask, jsonify, request
from flask_cors import CORS

# Configure logging so errors appear clearly in Render's log dashboard
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)


def extract_best_url(info: dict) -> str:
    """
    Aggressively search for the best playable video URL from yt-dlp info dict.
    Priority order:
      1. Root-level 'url' (single-format extractors)
      2. Root-level 'manifest_url' (HLS/DASH streams)
      3. Best combined video+audio format from formats list
      4. Best video-only format (highest resolution) from formats list
      5. Absolute fallback: first format that has any url
    """
    # 1. Root-level direct URL
    if info.get("url"):
        logger.info("URL found at root level.")
        return info["url"]

    # 2. Root-level manifest (HLS/DASH)
    if info.get("manifest_url"):
        logger.info("Manifest URL found at root level.")
        return info["manifest_url"]

    formats = info.get("formats", [])
    if not formats:
        logger.warning("No formats list found in info dict.")
        return ""

    logger.info("Searching formats list. Total formats available: %d", len(formats))

    # Log every format for deep debugging in Render logs
    for i, f in enumerate(formats):
        logger.info(
            "Format[%d] -> id=%s | ext=%s | vcodec=%s | acodec=%s | height=%s | url_present=%s | manifest=%s",
            i,
            f.get("format_id"),
            f.get("ext"),
            f.get("vcodec"),
            f.get("acodec"),
            f.get("height"),
            bool(f.get("url")),
            bool(f.get("manifest_url")),
        )

    # 3. Best combined (has both video and audio) — prefer mp4, then any ext
    combined = [
        f for f in formats
        if f.get("url")
        and f.get("vcodec", "none") != "none"
        and f.get("acodec", "none") != "none"
    ]
    if combined:
        # Prefer mp4 combined first
        mp4_combined = [f for f in combined if f.get("ext") == "mp4"]
        pool = mp4_combined if mp4_combined else combined
        best = max(pool, key=lambda f: f.get("height") or 0)
        logger.info("Selected combined format: id=%s ext=%s height=%s", best.get("format_id"), best.get("ext"), best.get("height"))
        return best["url"]

    # 4. Best video-only (highest resolution) — for muxed streams via manifest
    video_only = [
        f for f in formats
        if f.get("url")
        and f.get("vcodec", "none") != "none"
    ]
    if video_only:
        best = max(video_only, key=lambda f: f.get("height") or 0)
        logger.info("Selected video-only format: id=%s ext=%s height=%s", best.get("format_id"), best.get("ext"), best.get("height"))
        return best["url"]

    # 4b. Manifest URL fallback inside formats
    manifest_formats = [f for f in formats if f.get("manifest_url")]
    if manifest_formats:
        best = manifest_formats[-1]
        logger.info("Selected manifest format: id=%s", best.get("format_id"))
        return best["manifest_url"]

    # 5. Absolute last resort: first format with any url
    for f in formats:
        if f.get("url"):
            logger.warning("Fallback: using first available format url. id=%s", f.get("format_id"))
            return f["url"]

    logger.error("Exhausted all strategies. No playable URL found.")
    return ""


@app.route("/")
def health_check():
    return jsonify({"status": "Active", "message": "Linkb API is running"})


@app.route("/api/download")
def download():
    url = request.args.get("url", "").strip()

    if not url:
        return jsonify({"success": False, "error": "No URL provided"}), 400

    logger.info("Download request received for URL: %s", url)

    ydl_opts = {
        "quiet": True,
        "no_warnings": False,       # Allow warnings to surface in logs
        "noplaylist": True,
        "skip_download": True,
        # No ext restriction — accept ANY format so we never get a 404 on extraction
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        # Helps with age-gated or region-locked content
        "extractor_args": {
            "youtube": {
                "skip": ["hls", "dash"],  # Prefer direct HTTP URLs over manifests on YouTube
            }
        },
        # Spoof a browser to avoid bot-detection 403s
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Unwrap playlist wrapper if noplaylist didn't fully flatten it
        if info.get("_type") == "playlist":
            entries = info.get("entries", [])
            if not entries:
                return jsonify({"success": False, "error": "Playlist is empty or inaccessible"}), 422
            info = entries[0]
            logger.info("Unwrapped playlist entry[0].")

        title = info.get("title", "Unknown Title")
        thumbnail = info.get("thumbnail", "")

        download_url = extract_best_url(info)

        if not download_url:
            logger.error("No download URL extracted. Full info keys: %s", list(info.keys()))
            return jsonify({
                "success": False,
                "error": "Could not extract a playable URL. The platform may use DRM or require authentication."
            }), 422

        logger.info("Successfully extracted URL for: %s", title)

        return jsonify({
            "success": True,
            "title": title,
            "thumbnail": thumbnail,
            "download_url": download_url,
        })

    except yt_dlp.utils.DownloadError as e:
        logger.error("DownloadError for %s -> %s", url, str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Download error: {str(e)}"}), 422

    except yt_dlp.utils.ExtractorError as e:
        logger.error("ExtractorError for %s -> %s", url, str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Extractor error: {str(e)}"}), 422

    except yt_dlp.utils.GeoRestrictionError as e:
        logger.error("GeoRestrictionError for %s -> %s", url, str(e))
        return jsonify({"success": False, "error": "This content is geo-restricted and cannot be accessed from the server's region."}), 451

    except yt_dlp.utils.UserNotLive as e:
        logger.error("UserNotLive for %s -> %s", url, str(e))
        return jsonify({"success": False, "error": "The requested live stream is not currently active."}), 422

    except Exception as e:
        logger.error("Unexpected error for %s -> %s", url, str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Unexpected server error: {str(e)}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
