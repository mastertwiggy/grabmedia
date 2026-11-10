"""Worker RQ: processa i job di download e pulisce i file vecchi."""
import logging
import os
import socket
import threading

from rq import Queue, SimpleWorker, Worker

from . import config, database, downloader

log = logging.getLogger("grabmedia.worker")


def process_download(job_id: str, url: str, fmt: str, audio_bitrate: int | None = None) -> dict:
    """Funzione eseguita da RQ per ogni job."""
    log.info("Job %s: inizio (%s, %s, bitrate=%s)", job_id, fmt, url, audio_bitrate)
    database.set_status(job_id, "running")
    database.set_progress(job_id, 0)
    try:
        result = downloader.download_url(url, fmt, job_id, audio_bitrate=audio_bitrate)
    except downloader.DownloadError as exc:
        log.warning("Job %s fallito: %s", job_id, exc)
        database.set_status(job_id, "failed", error_message=str(exc))
        downloader.cleanup_job_dir(job_id)
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Job %s: errore inatteso", job_id)
        database.set_status(
            job_id, "failed", error_message=f"Errore interno: {type(exc).__name__}: {exc}"
        )
        downloader.cleanup_job_dir(job_id)
        raise

    database.set_result(job_id, result["filename"], result["path"], result["size"])
    database.update_job(job_id, effective_format=result["format"])
    database.set_progress(job_id, 100)
    database.set_status(job_id, "completed")
    log.info("Job %s: completato -> %s", job_id, result["filename"])
    return {"filename": result["filename"], "size": result["size"]}


def _cleanup_loop(stop: threading.Event) -> None:
    """Thread in background: cancella job e file più vecchi di FILE_TTL_HOURS."""
    while not stop.is_set():
        try:
            database.cleanup_old_jobs(config.FILE_TTL_HOURS)
        except Exception:  # noqa: BLE001
            log.exception("Errore durante la pulizia automatica")
        stop.wait(config.CLEANUP_INTERVAL_SECONDS)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    connection = database.get_raw_redis()
    queue = Queue(config.QUEUE_NAME, connection=connection)

    stop = threading.Event()
    threading.Thread(target=_cleanup_loop, args=(stop,), daemon=True, name="cleanup").start()

    worker_cls = SimpleWorker if os.name == "nt" else Worker
    worker = worker_cls(
        [queue],
        connection=connection,
        name=f"grabmedia-{socket.gethostname()}-{os.getpid()}",
    )
    log.info("Worker avviato sulla coda '%s' (download dir: %s)", config.QUEUE_NAME, config.DOWNLOAD_DIR)
    try:
        worker.work(with_scheduler=False)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
