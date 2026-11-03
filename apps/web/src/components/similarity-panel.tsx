"use client";

import { useCallback, useEffect, useState } from "react";
import { LoaderCircle, ScanSearch } from "lucide-react";
import { Analysis, AnalysisMode, Corpus, Production, duration, request } from "@/lib/api";

export function SimilarityPanel({ production }: { production: Production }) {
  const [corpus, setCorpus] = useState<Corpus | null>(null);
  const [analyses, setAnalyses] = useState<Analysis[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [epoch, setEpoch] = useState(0);
  const [mode, setMode] = useState<AnalysisMode>("chroma");
  const visibleAnalyses = analyses.filter((row) => row.mode === mode);
  const selected = visibleAnalyses.find((row) => row.id === selectedId) ?? visibleAnalyses[0];
  const active = analyses.some((row) => row.status === "queued" || row.status === "running");

  const refresh = useCallback(async (signal: AbortSignal) => {
    const [catalog, history] = await Promise.all([
      request<Corpus>("/api/v1/corpus", { signal }),
      request<{ items: Analysis[] }>(`/api/v1/productions/${production.id}/analyses`, { signal }),
    ]);
    if (signal.aborted) return false;
    setCorpus(catalog); setAnalyses(history.items); setError("");
    return history.items.some((row) => row.status === "queued" || row.status === "running");
  }, [production.id]);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      let delay = 10000;
      try { if (await refresh(controller.signal)) delay = 1500; }
      catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "검사 상태를 확인할 수 없습니다."); }
      finally { if (!controller.signal.aborted) timer = setTimeout(poll, delay); }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [refresh, epoch]);

  async function inspect() {
    if (submitting || active || !corpus?.available) return;
    setSubmitting(true); setError("");
    try {
      const row = await request<Analysis>(`/api/v1/productions/${production.id}/analyses`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }),
      });
      setAnalyses((previous) => [row, ...previous.filter((item) => item.id !== row.id)]);
      setSelectedId(row.id); setEpoch((previous) => previous + 1);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "검사를 요청하지 못했습니다."); }
    finally { setSubmitting(false); }
  }

  return <section className="panel similarity-panel" aria-labelledby="similarity-heading">
    <div className="panel-header"><h2 id="similarity-heading"><ScanSearch size={17} /> 유사 구간 검사</h2><span className="micro-label">V{production.version_no}</span></div>
    <div className="similarity-body">
      <p className="field-help">{corpus ? `${corpus.source} ${corpus.tracks}곡의 30초 클립과 비교합니다.` : "비교 DB를 확인하고 있습니다."} 원하는 버전에서 직접 실행할 수 있습니다.</p>
      <label className="analysis-history-label">검사 방식<select aria-label="검사 방식" value={mode} disabled={submitting} onChange={(event) => setMode(event.target.value as AnalysisMode)}>
        <option value="chroma">기존 음악 특징 검색</option><option value="melody_rhythm">선율·음 길이 비교 · 실험</option>
      </select></label>
      {mode === "melody_rhythm" ? <p className="analysis-notice">실제 보컬로 검증하기 전인 실험 방식입니다. 선율 후보의 음정 변화와 음 길이를 나눠 비교합니다. 첫 검사는 비교곡의 선율 추출로 더 오래 걸릴 수 있습니다.</p> : null}
      <button type="button" className="button secondary inspection-button" onClick={() => void inspect()} disabled={!corpus?.available || active || submitting}>
        {active || submitting ? <LoaderCircle size={16} className="spin" /> : <ScanSearch size={16} />}
        {submitting ? "검사 접수 중…" : active ? "백그라운드 검사 중…" : selected?.status === "ready" && selected.corpus_id === corpus?.snapshot_id && selected.algorithm_version === corpus?.algorithms[mode] ? "동일 조건 결과 확인" : selected?.status === "failed" ? "다시 검사" : `V${production.version_no} 유사도 검사`}
      </button>
      {corpus && !corpus.available ? <p className="field-help">비교 음원 DB가 아직 준비되지 않았습니다.</p> : null}
      {error ? <p role="alert" className="form-error">{error}</p> : null}
      <div aria-live="polite">
        {selected?.status === "queued" || selected?.status === "running" ? <p className="field-help">{selected.status === "queued" ? "검사 대기 중입니다. 분석 워커가 순서대로 처리합니다." : "유사한 곡과 구간을 찾고 있습니다. 다른 버전을 재생하거나 수정할 수 있습니다."}</p> : null}
        {selected?.status === "failed" ? <p role="alert" className="form-error">{selected.error_message}</p> : null}
      </div>
      {active && selected?.status !== "queued" && selected?.status !== "running" ? <p className="field-help">이 버전의 다른 검사 방식이 처리 중입니다.</p> : null}
      {visibleAnalyses.length > 1 ? <label className="analysis-history-label">검사 이력<select aria-label="검사 이력" value={selected?.id ?? ""} onChange={(event) => setSelectedId(event.target.value)}>{visibleAnalyses.map((row) => <option key={row.id} value={row.id}>{new Date(row.created_at).toLocaleString("ko-KR")} · {row.corpus_tracks}곡 · {row.status}</option>)}</select></label> : null}
      {selected?.status === "ready" && selected.result ? <div className="similarity-results">
        <div className="analysis-summary"><strong>후보 {selected.result.matches.length}곡</strong><span>{selected.result.reviewed_tracks !== undefined ? `${selected.result.reviewed_tracks}/${selected.corpus_tracks}곡 검토` : `${selected.corpus_tracks}곡 비교`} · {((selected.elapsed_ms ?? 0) / 1000).toFixed(2)}초</span></div>
        <p className="analysis-notice">{selected.result.notice}</p>
        {selected.result.coverage_status === "partial" ? <p className="empty-matches">일부 비교곡 또는 입력 구간에서 선율 후보를 추출하지 못했습니다. 전체 범위를 검토한 결과가 아닙니다.{selected.result.unavailable_references?.length ? ` 비교곡 ${selected.result.unavailable_references.length}곡 미검토.` : ""}</p> : null}
        {production.provider === "demo" ? <p className="field-help">현재 입력은 합성 연주음 데모입니다. 실제 보컬 노래의 검사 품질 검증과 구분합니다.</p> : null}
        {selected.result.matches.length === 0 ? <p className="empty-matches">{mode === "melody_rhythm" ? "검토 가능한 선율 구간에서 후보를 찾지 못했습니다." : "현재 비교 DB와 검색 기준에서 후보를 찾지 못했습니다."}</p> : selected.result.matches.map((match, index) => <article className="similarity-match" key={match.track_id}>
          <div className="match-heading"><span className="match-rank">{index + 1}</span><div><h3>{match.title}</h3><p>{match.artist}{match.genre ? ` · ${match.genre}` : ""}</p></div><span className="match-distance">{match.melody_distance !== undefined && match.rhythm_distance !== undefined ? <>선율 {match.melody_distance.toFixed(3)}<br />음 길이 {match.rhythm_distance.toFixed(3)}</> : <>거리 {match.distance.toFixed(3)}</>}</span></div>
          {match.evidence_type ? <p className="field-help">{match.evidence_type === "melody_and_duration" ? "선율 후보와 음 길이 패턴이 함께 가까운 구간" : "선율 후보는 가깝지만 음 길이 패턴에는 차이가 있는 구간"}</p> : null}
          <div className="match-players">
            <ClipPlayer key={`${production.id}-${match.query_start}`} src={production.audio_url!} start={match.query_start} end={match.query_end} label={`내 V${production.version_no}`} />
            <ClipPlayer key={`${match.track_id}-${match.reference_start}`} src={match.audio_url} start={match.reference_start} end={match.reference_end} label="비교 클립" />
          </div>
          <p className="match-source"><a href={match.source_url} target="_blank" rel="noreferrer">음원 출처</a><a href={match.license_url} target="_blank" rel="noreferrer">이용 조건</a><span>비교 시각은 FMA 클립 기준</span></p>
        </article>)}
        {selected.result.matches.length ? <p className="field-help">거리가 낮을수록 분석한 음악적 특징이 가깝습니다. 순위와 거리는 검토할 후보를 고르기 위한 정보입니다.</p> : null}
      </div> : null}
    </div>
  </section>;
}

function ClipPlayer({ src, start, end, label }: { src: string; start: number; end: number; label: string }) {
  return <div className="match-player"><span>{label} · {duration(start)}–{duration(end)}</span><audio controls preload="none" src={src} aria-label={`${label} ${duration(start)}부터 ${duration(end)} 비교 재생`}
    onLoadedMetadata={(event) => { event.currentTarget.currentTime = start; }}
    onPlay={(event) => { const audio = event.currentTarget; if (audio.readyState && (audio.currentTime < start || audio.currentTime >= end)) audio.currentTime = start; }}
    onTimeUpdate={(event) => { if (event.currentTarget.currentTime >= end) event.currentTarget.pause(); }} /></div>;
}
