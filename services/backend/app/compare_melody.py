"""Compare two local files with the experimental melody/duration baseline; no network calls."""
import argparse
import hashlib
import json
import math
from pathlib import Path

import soundfile as sf

from . import melody
from .config import Settings


def calibration_from(path):
    report = json.loads(Path(path).read_text())
    actual_hash = hashlib.sha256(Path(melody.__file__).read_bytes()).hexdigest()
    if report.get("method_version") != melody.METHOD_VERSION or report.get("method_sha256") != actual_hash:
        raise ValueError("현재 추출·비교 코드와 보정 보고서의 버전이 다릅니다. 같은 코드로 평가한 보고서를 사용해 주세요.")
    thresholds = [float(report[key]) for key in ("melody_threshold", "rhythm_threshold")]
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in thresholds):
        raise ValueError("보정 보고서의 임계값이 올바르지 않습니다.")
    return report, thresholds


def compare_files(query_path, reference_path, calibration_path):
    report, (melody_threshold, rhythm_threshold) = calibration_from(calibration_path)
    try:
        query, reference = melody.extract_notes(query_path), melody.extract_notes(reference_path)
        result = melody.compare_notes(query, reference, melody_threshold, rhythm_threshold)
        result.update(query_notes=len(query.pitches), reference_notes=len(reference.pitches))
    except (melody.MelodyUnavailable, sf.LibsndfileError) as exc:
        return {"status": "insufficient_features", "comparison": None, "reason": str(exc),
                "notice": "선율 후보를 충분히 추출하지 못했습니다. 유사하지 않다는 결과가 아닙니다."}
    return {"status": "ready", "comparison": result,
        "query_extraction": {"notes": len(query.pitches), "coverage": round(query.coverage, 4)},
        "reference_extraction": {"notes": len(reference.pitches), "coverage": round(reference.coverage, 4)},
        "calibration": {"generator_version": report["generator_version"], "method_version": report["method_version"],
            "method_sha256": report["method_sha256"], "melody_threshold": melody_threshold,
            "rhythm_threshold": rhythm_threshold, "source": "development compositions only"},
        "notice": "실험용 선율 후보·음 길이 비교입니다. 보컬 분리·실제 주선율 추출·법적 표절 여부는 검증하거나 판정하지 않습니다."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--calibration", type=Path)
    args = parser.parse_args()
    calibration = args.calibration or Path(__file__).with_name("melody_calibration.json")
    try:
        result = compare_files(args.query, args.reference, calibration)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f"비교 설정을 확인해 주세요: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "ready":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
