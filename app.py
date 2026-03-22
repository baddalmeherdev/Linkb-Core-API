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

# Strictly merged: video+audio together. Falls back progressively.
FORMAT_PREFERENCE = "/".join([
    "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]",
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]",
    "bestvideo+bestaudio",
    "best[ext=mp4]",
    "best",
])

# Labels that map a vertical resolution to a human-readable quality name
QUALITY_LABELS = {
    2160: "4K",
    1440: "2K",
    1080: "1080p",
    720:  "720p",
    480:  "480p",
    360:  "360p",
    240:  "240p",
    144:  "144p",
}


# ── Helpers ────────────────────────────────────────────────────────────────────
def label_for_height(height: int | None) -> str:
    """Return a display label like '720p' or '4K' for a given pixel height."""
    if height is None:
        return "Unknown"
    # Snap to the nearest standard label
    for threshold, label in sorted(QUALITY_LABELS.items(), reverse=True):
        if height >= threshold:
            return label
    return f"{height}p"


def extract_best_url(info: dict) -> str:
    """
    Walk the yt-dlp info dict through a 6-step priority chain and return
    the best available video+audio URL. Never raises — returns '' on failure.
    """
    # Step 1 — Root-level direct URL
    if info.get("url"):
        logger.info("URL: Step 1 — root-level url")
        return info["url"]

    # Step 2 — Root-level HLS/DASH manifest
    if info.get("manifest_url"):
        logger.info("URL: Step 2 — root-level manifest_url")
        return info["manifest_url"]

    formats = info.get("formats", [])
    if not formats:
        logger.warning("URL: No formats list found.")
        return ""

    logger.info("URL: scanning %d formats for best url...", len(formats))

    # Step 3 — Best combined mp4 (video+audio, single file)
    combined_mp4 = [
        f for f in formats
        if f.get("url")
        and f.get("ext") == "mp4"
        and f.get("vcodec", "none") != "none"
        and f.get("acodec", "none") != "none"
    ]
    if combined_mp4:
        best = max(combined_mp4, key=lambda f: f.get("height") or 0)
        logger.info("URL: Step 3 — combined mp4 id=%s h=%s", best.get("format_id"), best.get("height"))
        return best["url"]

    # Step 4 — Best combined any extension
    combined_any = [
        f for f in formats
        if f.get("url")
        and f.get("vcodec", "none") != "none"
        and f.get("acodec", "none") != "none"
    ]
    if combined_any:
        best = max(combined_any, key=lambda f: f.get("height") or 0)
        logger.info("URL: Step 4 — combined any ext id=%s h=%s", best.get("format_id"), best.get("height"))
        return best["url"]

    # Step 5 — Best video-only (highest resolution)
    video_only = [
        f for f in formats
        if f.get("url") and f.get("vcodec", "none") != "none"
    ]
    if video_only:
        best = max(video_only, key=lambda f: f.get("height") or 0)
        logger.info("URL: Step 5 — video-only id=%s h=%s", best.get("format_id"), best.get("height"))
        return best["url"]

    # Step 5b — Per-format manifest URL
    for f in formats:
        if f.get("manifest_url"):
            logger.info("URL: Step 5b — per-format manifest id=%s", f.get("format_id"))
            return f["manifest_url"]

    # Step 6 — Absolute last resort
    for f in formats:
        if f.get("url"):
            logger.warning("URL: Step 6 — last resort id=%s", f.get("format_id"))
            return f["url"]

    logger.error("URL: All steps failed. No playable URL found.")
    return ""


def build_format_list(info: dict) -> list[dict]:
    """
    Build a clean, deduplicated list of quality options for the Android
    Quality Picker UI. Each entry contains a label, resolution, file size
    (if known), extension, and a direct URL.

    Inclusion rules (in priority order):
      1. Combined formats  — have BOTH vcodec and acodec (ideal, plays anywhere)
      2. Video-only formats — included as a fallback so the picker is never empty,
         flagged with has_audio=False so the client can warn the user
    Excluded:
      - Audio-only tracks
      - Formats with no direct URL (e.g. pure DASH manifests without a url key)
      - Duplicate resolutions (only the best bitrate per height is kept)
    """
    formats = info.get("formats", [])
    if not formats:
        return []

    seen_heights: dict[str, dict] = {}   # key: "label|ext", value: best format so far

    for f in formats:
        url = f.get("url")
        if not url:
            continue

        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        has_video = vcodec != "none"
        has_audio = acodec != "none"

        # Skip audio-only streams entirely
        if not has_video:
            continue

        height   = f.get("height")
        ext      = f.get("ext", "mp4")
        tbr      = f.get("tbr") or 0       # total bitrate — used to pick the best among dupes
        filesize = f.get("filesize") or f.get("filesize_approx")

        label = label_for_height(height)
        dedup_key = f"{label}|{ext}|{'av' if has_audio else 'v'}"

        # Keep only the highest-bitrate format per (label, ext, audio-presence) bucket
        if dedup_key not in seen_heights or tbr > (seen_heights[dedup_key].get("tbr") or 0):
            seen_heights[dedup_key] = {
                "quality_label": label,
                "height":        height,
                "ext":           ext,
                "has_audio":     has_audio,
                "filesize":      filesize,          # bytes or None
                "format_id":     f.get("format_id"),
                "tbr":           tbr,
                "url":           url,
            }

    if not seen_heights:
        return []

    # Sort: combined (has_audio) first, then by height descending
    results = sorted(
        seen_heights.values(),
        key=lambda f: (f["has_audio"], f["height"] or 0),
        reverse=True,
    )

    # Strip internal fields not needed by the client
    return [
        {
            "quality_label": r["quality_label"],
            "height":        r["height"],
            "ext":           r["ext"],
            "has_audio":     r["has_audio"],
            "filesize":      r["filesize"],
            "url":           r["url"],
        }
        for r in results
    ]


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def health_check():
    """UptimeRobot pings this every 5 min to keep Render's free tier alive."""
    return jsonify({"status": "Active", "message": "Linkb API is running"})


@app.route("/api/download")
def download():
    url = request.args.get("url", "").strip()

    if not url:
        logger.warning("Request with no URL parameter.")
        return jsonify({"success": False, "error": "Missing required query parameter: url"}), 400

    logger.info("=== Download request: %s ===", url)

    ydl_opts = {
        # ── Core ─────────────────────────────────────────────────────────
        "quiet":         True,
        "no_warnings":   False,     # Surface yt-dlp warnings into our logger
        "skip_download": True,      # NEVER write video bytes to disk
        "noplaylist":    True,      # Single video only

        # ── Format (merged video+audio strictly preferred) ────────────────
        "format": FORMAT_PREFERENCE,

        # ── Bot-bypass ───────────────────────────────────────────────────
        "http_headers": BROWSER_HEADERS,

        # ── Platform tweaks ──────────────────────────────────────────────
        "extractor_args": {
            # Prefer direct HTTP URLs on YouTube — avoids returning an
            # HLS manifest that the Android client can't easily seek
            "youtube": {"skip": ["hls", "dash"]},
        },

        # ── Robustness ───────────────────────────────────────────────────
        "socket_timeout":    30,
        "retries":            3,
        "fragment_retries":   3,
        "ignoreerrors":      False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Unwrap a playlist wrapper that sometimes leaks through noplaylist
        if info.get("_type") == "playlist":
            entries = [e for e in info.get("entries", []) if e]
            if not entries:
                return jsonify({"success": False, "error": "Playlist is empty or inaccessible."}), 422
            info = entries[0]
            logger.info("Unwrapped playlist; using first entry.")

        # ── Core metadata ─────────────────────────────────────────────────
        title       = info.get("title")          or "Unknown Title"
        thumbnail   = info.get("thumbnail")      or ""
        description = info.get("description")    or ""   # captions / hashtags
        duration    = info.get("duration")        or None  # seconds
        platform    = info.get("extractor_key")  or info.get("extractor") or "unknown"
        uploader    = info.get("uploader")        or info.get("channel") or ""
        view_count  = info.get("view_count")      or None
        like_count  = info.get("like_count")      or None

        # ── Primary download URL (best merged video+audio) ────────────────
        download_url = extract_best_url(info)

        if not download_url:
            logger.error("No playable URL found. Info keys: %s", list(info.keys()))
            return jsonify({
                "success": False,
                "error": (
                    "No playable URL could be extracted. "
                    "The content may be DRM-protected, private, or require login."
                ),
            }), 422

        # ── Quality picker list ───────────────────────────────────────────
        format_list = build_format_list(info)
        logger.info(
            "Success — platform=%s title=%s formats=%d",
            platform, title, len(format_list),
        )

        return jsonify({
            "success":      True,
            "platform":     platform,
            "title":        title,
            "description":  description,
            "thumbnail":    thumbnail,
            "duration":     duration,
            "uploader":     uploader,
            "view_count":   view_count,
            "like_count":   like_count,
            "download_url": download_url,   # Best merged video+audio URL
            "formats":      format_list,    # Full quality picker list
        })

    # ── yt-dlp specific exceptions ─────────────────────────────────────────────
    except yt_dlp.utils.DownloadError as e:
        logger.error("DownloadError: %s", str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Download error: {str(e)}"}), 422

    except yt_dlp.utils.ExtractorError as e:
        logger.error("ExtractorError: %s", str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Extractor error: {str(e)}"}), 422

    except yt_dlp.utils.GeoRestrictionError as e:
        logger.error("GeoRestrictionError: %s", str(e))
        return jsonify({
            "success": False,
            "error": "Content is geo-restricted and cannot be accessed from this server's region.",
        }), 451

    except yt_dlp.utils.UserNotLive as e:
        logger.error("UserNotLive: %s", str(e))
        return jsonify({"success": False, "error": "This live stream is not currently active."}), 422

    except yt_dlp.utils.UnsupportedError as e:
        logger.error("UnsupportedError: %s", str(e))
        return jsonify({"success": False, "error": f"Unsupported URL or platform: {str(e)}"}), 422

    except yt_dlp.utils.PostProcessingError as e:
        logger.error("PostProcessingError: %s", str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Post-processing error: {str(e)}"}), 500

    except Exception as e:
        logger.error("Unhandled exception for %s: %s", url, str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Unexpected server error: {str(e)}"}), 500


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
