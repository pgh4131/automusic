"""Fetch a bounded FMA Small subset from the official ZIP using HTTP byte ranges."""
import argparse
import csv
import io
import json
import re
import shutil
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from sqlalchemy import select

import httpx

from .config import Settings
from .db import make_database
from .models import CorpusTrack
from .similarity import FEATURE_VERSION, extract, file_hash, save_features

BASE_URL = "https://os.unil.cloud.switch.ch/fma/"
SOURCE_URL = "https://github.com/mdeff/fma"
MAX_MEMBER_BYTES = 120 * 1024 * 1024


class RangeFile(io.RawIOBase):
    """A seekable, read-only ZIP source. Refuse a server that ignores Range."""
    def __init__(self, client, url):
        self.client, self.url, self.position, self.transferred = client, url, 0, 0
        with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
            if response.status_code != 206:
                raise ValueError("서버가 부분 다운로드를 지원하지 않습니다. 전체 ZIP을 자동 다운로드하지 않습니다.")
            match = re.fullmatch(r"bytes 0-0/(\d+)", response.headers.get("content-range", ""))
            if not match:
                raise ValueError("잘못된 Range 응답입니다.")
            self.size = int(match[1])
        self.cache = (0, b"")

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        self.position = offset if whence == 0 else (self.position if whence == 1 else self.size) + offset
        if self.position < 0:
            raise ValueError("negative seek")
        return self.position

    def read(self, size=-1):
        size = min(self.size - self.position, size if size >= 0 else self.size)
        if size <= 0:
            return b""
        if size > MAX_MEMBER_BYTES:
            raise ValueError("개별 파일의 다운로드 한도를 초과했습니다.")
        start, cached = self.cache
        if not (start <= self.position and self.position + size <= start + len(cached)):
            start = self.position
            end = min(self.size - 1, start + max(size, 1024 * 1024) - 1)
            with self.client.stream("GET", self.url, headers={"Range": f"bytes={start}-{end}"}) as response:
                if response.status_code != 206 or response.headers.get("content-range") != f"bytes {start}-{end}/{self.size}":
                    raise ValueError("서버의 Range 응답이 요청과 다릅니다.")
                blocks, received = [], 0
                for block in response.iter_bytes():
                    received += len(block)
                    if received > end - start + 1:
                        raise ValueError("서버가 요청한 범위를 넘어 파일을 전송했습니다.")
                    blocks.append(block)
                cached = b"".join(blocks)
            if len(cached) != end - start + 1:
                raise ValueError("부분 파일이 잘렸습니다.")
            self.transferred += len(cached)
            self.cache = (start, cached)
        offset = self.position - start
        self.position += size
        return cached[offset:offset + size]


def license_url(value):
    # Exact allowlist. No inference from words such as "free" or from code/metadata licenses.
    value = value.strip()
    if value in {"CC0 1.0 Universal", "https://creativecommons.org/publicdomain/zero/1.0/"}:
        return "https://creativecommons.org/publicdomain/zero/1.0/"
    for version in ("1.0", "2.0", "2.5", "3.0", "4.0"):
        if value in {f"Attribution {version} International", f"https://creativecommons.org/licenses/by/{version}/",
                     f"http://creativecommons.org/licenses/by/{version}/"}:
            return f"https://creativecommons.org/licenses/by/{version}/"
    return None


def metadata_rows(raw):
    csv.field_size_limit(10 * 1024 * 1024)
    with Path(raw).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        first, second = next(reader), next(reader)
        headers = list(zip(first, second))
        for row in reader:
            if not row or not row[0].isdigit():
                continue
            yield int(row[0]), {key: value for key, value in zip(headers, row)}


def source_url(value):
    parsed = urlsplit(value)
    return value if parsed.scheme in {"http", "https"} and parsed.hostname in {"freemusicarchive.org", "www.freemusicarchive.org"} else SOURCE_URL


def prepare(settings, limit=100):
    if not 1 <= limit <= 1000:
        raise ValueError("한 번에 준비할 수 있는 곡 수는 1~1000곡입니다.")
    root = settings.data_dir / "corpus"
    root.mkdir(parents=True, exist_ok=True)
    engine, sessions = make_database(settings)
    report = {"source": SOURCE_URL, "subset": "FMA Small", "requested_tracks": limit,
        "accepted_licenses": "exact CC BY International / CC0 allowlist", "items": [], "errors": []}
    try:
        with httpx.Client(timeout=90, follow_redirects=True, headers={"Accept-Encoding": "identity"}) as client:
            metadata_file = root / "tracks.csv"
            if not metadata_file.exists():
                remote = RangeFile(client, BASE_URL + "fma_metadata.zip")
                with zipfile.ZipFile(remote) as archive:
                    member = next(info for info in archive.infolist() if info.filename.endswith("/tracks.csv"))
                    if member.file_size > 512 * 1024 * 1024:
                        raise ValueError("메타데이터가 허용 크기를 초과합니다.")
                    partial = metadata_file.with_suffix(".part")
                    with archive.open(member) as source, partial.open("wb") as destination:
                        shutil.copyfileobj(source, destination)  # zipfile verifies member CRC.
                    partial.replace(metadata_file)
                report["metadata_download_bytes"] = remote.transferred
            report["metadata_sha256"] = file_hash(metadata_file)
            remote = RangeFile(client, BASE_URL + "fma_small.zip")
            with zipfile.ZipFile(remote) as archive:
                audio_members = {int(Path(info.filename).stem): info for info in archive.infolist()
                    if info.filename.endswith(".mp3") and Path(info.filename).stem.isdigit()}
                for track_id, metadata in metadata_rows(metadata_file):
                    accepted_license = license_url(metadata.get(("track", "license"), ""))
                    if track_id not in audio_members or not accepted_license:
                        continue
                    identifier = f"fma-{track_id:06d}-{FEATURE_VERSION}"
                    with sessions() as session:
                        existing = session.get(CorpusTrack, identifier)
                        if existing:
                            audio_path = root / "audio" / existing.audio_filename
                            feature_path = root / "features" / existing.feature_filename
                            if not audio_path.is_file() or not feature_path.is_file() or file_hash(audio_path) != existing.audio_hash or file_hash(feature_path) != existing.feature_hash:
                                raise ValueError(f"기존 곡 {track_id}의 파일이 없거나 변경됐습니다.")
                            report["items"].append({"id": existing.id, "license_url": existing.license_url, "reused": True})
                            if len(report["items"]) >= limit:
                                break
                            continue
                    member = audio_members[track_id]
                    if member.file_size > 8 * 1024 * 1024:
                        report["errors"].append({"track": track_id, "error": "oversize"})
                        continue
                    audio_path = root / "audio" / f"{track_id:06d}.mp3"
                    feature_path = root / "features" / f"{track_id:06d}-{FEATURE_VERSION}.npz"
                    try:
                        audio_path.parent.mkdir(parents=True, exist_ok=True)
                        partial = audio_path.with_suffix(".part")
                        partial.write_bytes(archive.read(member))
                        partial.replace(audio_path)
                        chroma, seconds = extract(audio_path)
                        save_features(feature_path, chroma, seconds)
                        with sessions.begin() as session:
                            session.add(CorpusTrack(id=identifier, source="fma-small", source_id=str(track_id),
                                title=metadata.get(("track", "title"), "") or f"FMA {track_id}",
                                artist=metadata.get(("artist", "name"), "Unknown"),
                                genre=metadata.get(("track", "genre_top"), ""),
                                source_url=source_url(metadata.get(("track", "url"), "")),
                                license_url=accepted_license, audio_filename=audio_path.name,
                                feature_filename=feature_path.name, audio_hash=file_hash(audio_path),
                                feature_hash=file_hash(feature_path), feature_version=FEATURE_VERSION, duration=seconds))
                        report["items"].append({"id": identifier, "license_url": accepted_license, "zip_crc32": member.CRC})
                        if len(report["items"]) % 10 == 0:
                            print(f"FMA indexed {len(report['items'])}/{limit}", flush=True)
                    except (ValueError, RuntimeError, zipfile.BadZipFile) as exc:
                        report["errors"].append({"track": track_id, "error": str(exc)[:150]})
                    if len(report["items"]) >= limit:
                        break
            report["audio_download_bytes"] = remote.transferred
        report["indexed_tracks"] = len(report["items"])
        (root / "import-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({key: value for key, value in report.items() if key not in {"items", "errors"}}, ensure_ascii=False), flush=True)
        if len(report["items"]) < limit:
            print(f"현재 정확한 라이선스 허용 목록에 해당하는 {len(report['items'])}곡을 모두 준비했습니다.", flush=True)
    finally:
        engine.dispose()


def reindex_local(settings):
    """Keep old feature revisions; create a new immutable revision from already-downloaded audio."""
    engine, sessions = make_database(settings)
    try:
        with sessions() as session:
            tracks = list(session.scalars(select(CorpusTrack).where(CorpusTrack.source == "fma-small")))
        seen = set()
        for track in tracks:
            identifier = f"fma-{int(track.source_id):06d}-{FEATURE_VERSION}"
            if identifier in seen:
                continue
            seen.add(identifier)
            with sessions() as session:
                if session.get(CorpusTrack, identifier):
                    continue
            audio_path = settings.data_dir / "corpus" / "audio" / track.audio_filename
            if file_hash(audio_path) != track.audio_hash:
                raise ValueError("원본 음원이 변경됐습니다.")
            features, seconds = extract(audio_path)
            feature_path = settings.data_dir / "corpus" / "features" / f"{int(track.source_id):06d}-{FEATURE_VERSION}.npz"
            save_features(feature_path, features, seconds)
            with sessions.begin() as session:
                session.add(CorpusTrack(id=identifier, source=track.source, source_id=track.source_id,
                    title=track.title, artist=track.artist, genre=track.genre, source_url=track.source_url,
                    license_url=track.license_url, audio_filename=track.audio_filename,
                    feature_filename=feature_path.name, audio_hash=track.audio_hash,
                    feature_hash=file_hash(feature_path), feature_version=FEATURE_VERSION, duration=seconds))
        print(f"로컬 음원 {len(seen)}곡의 현재 특징 버전 준비 완료", flush=True)
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--reindex", action="store_true", help="다운로드 없이 기존 음원에서 새 특징 버전 생성")
    args = parser.parse_args()
    if args.reindex:
        reindex_local(Settings.load())
    else:
        prepare(Settings.load(), args.limit)


if __name__ == "__main__":
    main()
