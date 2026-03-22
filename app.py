import logging
import yt_dlp
from flask import Flask, jsonify, request
from flask_cors import CORS

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ── Constants ──────────────────────────────────────────────────────────────────
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Format preference order — tried left to right by yt-dlp
FORMAT_PREFERENCE = "/".join([
    "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]",  # Best 1080p mp4 + m4a audio
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]",                # Best mp4 + m4a audio (any res)
    "bestvideo+bestaudio",                                   # Best video + audio (any ext)
    "best[ext=mp4]",                                         # Single-file best mp4
    "best",                                                  # Absolute fallback
])


# ── URL Extraction Helper ──────────────────────────────────────────────────────
def extract_best_url(info: dict) -> str:
    """
    Walk the yt-dlp info dict using a 6-step priority chain to find the
    best playable video URL. Works universally across YouTube, Instagram,
    TikTok, Facebook, Twitter/X, and most other platforms.
    """

    # Step 1 — Root-level direct URL (works for most simple extractors)
    if info.get("url"):
        logger.info("Step 1 hit: root-level url found.")
        return info["url"]

    # Step 2 — Root-level HLS/DASH manifest
    if info.get("manifest_url"):
        logger.info("Step 2 hit: root-level manifest_url found.")
        return info["manifest_url"]

    formats = info.get("formats", [])

    if not formats:
        logger.warning("No formats list present in info dict.")
        return ""

    logger.info("Scanning %d formats...", len(formats))

    # Log every format for deep visibility in Render's log dashboard
    for i, f in enumerate(formats):
        logger.info(
            "  [%02d] id=%-12s ext=%-6s vcodec=%-20s acodec=%-20s "
            "height=%-6s url=%s manifest=%s",
            i,
            f.get("format_id", "?"),
            f.get("ext", "?"),
            f.get("vcodec", "none"),
            f.get("acodec", "none"),
            f.get("height", "?"),
            bool(f.get("url")),
            bool(f.get("manifest_url")),
        )

    # Step 3 — Best combined mp4 (video + audio in one file)
    combined_mp4 = [
        f for f in formats
        if f.get("url")
        and f.get("ext") == "mp4"
        and f.get("vcodec", "none") != "none"
        and f.get("acodec", "none") != "none"
    ]
    if combined_mp4:
        best = max(combined_mp4, key=lambda f: f.get("height") or 0)
        logger.info("Step 3 hit: combined mp4 — id=%s height=%s", best.get("format_id"), best.get("height"))
        return best["url"]

    # Step 4 — Best combined stream (any extension)
    combined_any = [
        f for f in formats
        if f.get("url")
        and f.get("vcodec", "none") != "none"
        and f.get("acodec", "none") != "none"
    ]
    if combined_any:
        best = max(combined_any, key=lambda f: f.get("height") or 0)
        logger.info("Step 4 hit: combined any ext — id=%s ext=%s height=%s", best.get("format_id"), best.get("ext"), best.get("height"))
        return best["url"]

    # Step 5 — Best video-only stream (highest resolution), audio will be missing
    # but at least gives a playable link for preview/download purposes
    video_only = [
        f for f in formats
        if f.get("url")
        and f.get("vcodec", "none") != "none"
    ]
    if video_only:
        best = max(video_only, key=lambda f: f.get("height") or 0)
        logger.info("Step 5 hit: video-only — id=%s ext=%s height=%s", best.get("format_id"), best.get("ext"), best.get("height"))
        return best["url"]

    # Step 5b — Manifest URL inside a format entry (HLS/DASH per-format)
    manifest_formats = [f for f in formats if f.get("manifest_url")]
    if manifest_formats:
        best = manifest_formats[-1]
        logger.info("Step 5b hit: per-format manifest_url — id=%s", best.get("format_id"))
        return best["manifest_url"]

    # Step 6 — Absolute last resort: first format with any URL at all
    for f in formats:
        if f.get("url"):
            logger.warning("Step 6 hit (last resort): id=%s ext=%s", f.get("format_id"), f.get("ext"))
            return f["url"]

    logger.error("All 6 extraction steps failed. No playable URL found.")
    return ""


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def health_check():
    """UptimeRobot pings this every 5 min to keep Render's free tier awake."""
    return jsonify({"status": "Active", "message": "Linkb API is running"})


@app.route("/api/download")
def download():
    url = request.args.get("url", "").strip()

    if not url:
        logger.warning("Request received with no URL parameter.")
        return jsonify({"success": False, "error": "Missing required query parameter: url"}), 400

    logger.info("=== New download request: %s ===", url)

    ydl_opts = {
        # ── Core behaviour ───────────────────────────────────────────────
        "quiet": True,
        "no_warnings": False,       # Surface warnings into our logger
        "skip_download": True,      # NEVER write video bytes to disk
        "noplaylist": True,         # Single video only, never a whole playlist

        # ── Format selection ─────────────────────────────────────────────
        "format": FORMAT_PREFERENCE,

        # ── Bot-bypass headers ───────────────────────────────────────────
        "http_headers": BROWSER_HEADERS,

        # ── Platform-specific tweaks ─────────────────────────────────────
        "extractor_args": {
            # Prefer direct HTTP URLs on YouTube; skip HLS/DASH manifests
            # so the returned URL is immediately downloadable by the client
            "youtube": {"skip": ["hls", "dash"]},
        },

        # ── Robustness ───────────────────────────────────────────────────
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "ignoreerrors": False,      # We want exceptions to bubble up
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Unwrap a playlist wrapper that sometimes leaks through noplaylist
        if info.get("_type") == "playlist":
            entries = [e for e in info.get("entries", []) if e]
            if not entries:
                return jsonify({"success": False, "error": "Playlist is empty or all entries are inaccessible."}), 422
            info = entries[0]
            logger.info("Unwrapped playlist; using first entry.")

        title     = info.get("title")     or "Unknown Title"
        thumbnail = info.get("thumbnail") or ""
        duration  = info.get("duration")  or None     # seconds, useful for the client
        platform  = info.get("extractor_key") or info.get("extractor") or "unknown"

        download_url = extract_best_url(info)

        if not download_url:
            logger.error(
                "Extraction returned no URL. Info keys present: %s", list(info.keys())
            )
            return jsonify({
                "success": False,
                "error": (
                    "No playable URL could be extracted. "
                    "The content may be DRM-protected, private, or require a login."
                ),
            }), 422

        logger.info("Success — platform=%s title=%s", platform, title)

        return jsonify({
            "success":      True,
            "platform":     platform,
            "title":        title,
            "thumbnail":    thumbnail,
            "duration":     duration,
            "download_url": download_url,
        })

    # ── Specific yt-dlp errors ─────────────────────────────────────────────────
    except yt_dlp.utils.DownloadError as e:
        # Covers: unavailable videos, private videos, deleted content, bad URLs
        logger.error("DownloadError: %s", str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Download error: {str(e)}"}), 422

    except yt_dlp.utils.ExtractorError as e:
        # Covers: unsupported sites, broken extractors, unexpected page structure
        logger.error("ExtractorError: %s", str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Extractor error: {str(e)}"}), 422

    except yt_dlp.utils.GeoRestrictionError as e:
        # Content is legally blocked in the server's region
        logger.error("GeoRestrictionError: %s", str(e))
        return jsonify({
            "success": False,
            "error": "This content is geo-restricted and cannot be accessed from the server's region.",
        }), 451  # HTTP 451 = Unavailable For Legal Reasons

    except yt_dlp.utils.UserNotLive as e:
        logger.error("UserNotLive: %s", str(e))
        return jsonify({"success": False, "error": "The requested live stream is not currently active."}), 422

    except yt_dlp.utils.UnsupportedError as e:
        logger.error("UnsupportedError: %s", str(e))
        return jsonify({"success": False, "error": f"Unsupported URL or platform: {str(e)}"}), 422

    except yt_dlp.utils.PostProcessingError as e:
        logger.error("PostProcessingError: %s", str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Post-processing error: {str(e)}"}), 500

    # ── Catch-all ──────────────────────────────────────────────────────────────
    except Exception as e:
        logger.error("Unhandled exception for URL %s: %s", url, str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Unexpected server error: {str(e)}"}), 500


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
