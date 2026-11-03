"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowDownToLine, ArrowRight, AudioLines, Check, ChevronDown, CircleHelp, Disc3, GitBranch, Headphones, History, LoaderCircle, Plus, Radio, Sparkles, WandSparkles, X } from "lucide-react";
import { Configuration, Production, Song, duration, isActive, request, statusLabel } from "@/lib/api";
import { SongLibrary, VersionHistory } from "./version-history";
import { SimilarityPanel } from "./similarity-panel";

const examples = [
  { label: "비 오는 밤", prompt: "비 오는 밤, 집으로 돌아가는 길의 조용한 위로를 담은 인디 팝. 따뜻한 기타와 부드러운 여성 보컬, 따라 부르기 좋은 후렴.", genre: "Indie pop", mood: "Dreamy" },
  { label: "여름의 끝", prompt: "여름의 마지막 드라이브를 떠올리는 청량한 신스 팝. 반짝이는 신디사이저와 경쾌한 드럼, 아쉬움과 설렘을 담은 보컬.", genre: "Synth pop", mood: "Nostalgic" },
  { label: "새로운 시작", prompt: "새로운 출발을 응원하는 밝은 어쿠스틱 팝. 리듬감 있는 기타와 힘 있는 보컬, 희망적인 가사와 기억에 남는 후렴.", genre: "Acoustic pop", mood: "Uplifting" },
];

export default function Studio() {
  const [config, setConfig] = useState<Configuration | null>(null);
  const [songs, setSongs] = useState<Song[]>([]);
  const [versions, setVersions] = useState<Production[]>([]);
  const [selectedSongId, setSelectedSongId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [revisionSource, setRevisionSource] = useState<Production | null>(null);
  const [pollEpoch, setPollEpoch] = useState(0);
  const [prompt, setPrompt] = useState("");
  const [title, setTitle] = useState("");
  const [genre, setGenre] = useState("Indie pop");
  const [mood, setMood] = useState("Dreamy");
  const [vocal, setVocal] = useState("Soft female vocals");
  const [language, setLanguage] = useState("Korean");
  const [seconds, setSeconds] = useState(30);
  const [lyricsMode, setLyricsMode] = useState<"auto" | "custom">("auto");
  const [lyrics, setLyrics] = useState("");
  const [provider, setProvider] = useState("elevenlabs");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [lastSynced, setLastSynced] = useState(false);
  const requestKey = useRef<{ fingerprint: string; key: string } | null>(null);

  const visibleVersions = versions.filter((item) => item.song_id === selectedSongId);
  const selected = visibleVersions.find((item) => item.id === selectedId) ?? visibleVersions[0] ?? null;
  const available = config?.providers.find((item) => item.id === provider)?.available ?? false;

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const [configuration, library] = await Promise.all([
      request<Configuration>("/api/v1/config", { signal }),
      request<{ items: Song[] }>("/api/v1/songs", { signal }),
    ]);
    const targetSongId = selectedSongId ?? library.items[0]?.id ?? null;
    const history = targetSongId ? await request<{ items: Production[] }>(`/api/v1/songs/${targetSongId}/versions`, { signal }) : { items: [] };
    if (signal?.aborted) return false;
    setConfig(configuration);
    setSongs(library.items);
    setVersions(history.items);
    setSelectedSongId(targetSongId);
    setConnectionError("");
    setLastSynced(true);
    return library.items.some((item) => isActive(item.latest_version.status)) || history.items.some((item) => isActive(item.status));
  }, [selectedSongId]);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      let delay = 10000;
      try { if (await refresh(controller.signal)) delay = 2000; }
      catch (cause) {
        if (!controller.signal.aborted) setConnectionError(cause instanceof Error ? cause.message : "연결 상태를 확인해 주세요.");
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, delay);
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [refresh, pollEpoch]);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting || !available) return;
    setError("");
    setSubmitting(true);
    const payload = {
      title: title.trim() || "Untitled session", prompt, genre, mood, vocal, language,
      duration_seconds: seconds, lyrics_mode: lyricsMode,
      lyrics: lyricsMode === "custom" ? lyrics : "", provider,
      ...(revisionSource ? { song_id: revisionSource.song_id, parent_production_id: revisionSource.id } : {}),
    };
    const fingerprint = JSON.stringify(payload);
    if (requestKey.current?.fingerprint !== fingerprint) {
      requestKey.current = { fingerprint, key: crypto.randomUUID() };
    }
    try {
      const item = await request<Production>("/api/v1/productions", {
        method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": requestKey.current.key },
        body: fingerprint,
      });
      setSongs((previous) => [{ id: item.song_id, title: item.song_title, version_count: item.version_no, latest_version: item }, ...previous.filter((song) => song.id !== item.song_id)]);
      setVersions((previous) => [item, ...previous.filter((version) => version.song_id === item.song_id && version.id !== item.id)]);
      setSelectedSongId(item.song_id);
      setSelectedId(item.id);
      setPollEpoch((previous) => previous + 1);
      setRevisionSource(null);
      requestKey.current = null;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "생성 요청에 실패했어요.");
    } finally { setSubmitting(false); }
  }

  function resetForm() {
    setPrompt(""); setTitle(""); setLyrics(""); setError("");
    setLyricsMode("auto"); setRevisionSource(null);
    requestKey.current = null;
    document.getElementById("prompt")?.focus();
  }

  function revise(version: Production) {
    setRevisionSource(version);
    setTitle(version.title); setPrompt(version.prompt); setGenre(version.genre);
    setMood(version.mood); setVocal(version.vocal); setLanguage(version.language);
    setSeconds(version.duration_seconds); setProvider(version.provider);
    setLyrics(version.lyrics);
    setLyricsMode(version.lyrics ? "custom" : "auto");
    setError(""); requestKey.current = null;
    document.getElementById("prompt")?.focus();
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <a href="#studio" className="brand" aria-label="AutoMusic 홈"><span className="brand-mark"><AudioLines size={23} /></span><span>auto<span className="brand-light">music</span><small>MAKE SOMETHING YOURS</small></span></a>
      <div className="workspace-label">YOUR WORKSPACE</div>
      <nav aria-label="주 메뉴">
        <a className="nav-item active" href="#studio"><WandSparkles size={18} /> 만들기 <span className="nav-tag">STUDIO</span></a>
        <a className="nav-item" href="#library"><History size={18} /> 내 음악 <span className="nav-count">{songs.length}</span></a>
      </nav>
      <div className="sidebar-note"><Radio size={19} /><p>아이디어가 음악이 되는 곳.</p><span>당신의 다음 소리를<br />여기서 시작하세요.</span></div>
      <div className="sidebar-footer"><span className="avatar">P</span><div>Personal workspace<small>LOCAL STUDIO · V0.2</small></div></div>
    </aside>

    <main id="studio">
      <header className="topbar"><div className="breadcrumb">Workspace <span>/</span> <strong>Music studio</strong></div><div className="connection"><i className={connectionError ? "dot offline" : lastSynced ? "dot" : "dot pending"} />{connectionError ? "API 연결 확인" : lastSynced ? "로컬 스튜디오 연결됨" : "연결 중"}</div></header>
      <div className="main-content">
        <div className="page-heading"><div><div className="eyebrow">FROM A THOUGHT TO A TRACK</div><h1>어떤 음악을 만들까요<span>?</span></h1><p>떠오른 장면을 적어보세요. 가사와 목소리를 더해 하나의 노래로.</p></div><button type="button" className="button secondary new-session" onClick={resetForm}><Plus size={16} /> 새 세션</button></div>
        {connectionError ? <div role="alert" className="banner error-banner">{connectionError}<button type="button" onClick={() => void refresh().catch(() => {})}>다시 연결</button></div> : null}
        <div className="studio-grid">
          <section className="composer panel" aria-labelledby="composer-heading">
            <div className="panel-header"><h2 id="composer-heading"><Sparkles size={17} />{revisionSource ? "수정본 만들기" : "새로운 트랙"}</h2><span className="micro-label">{revisionSource ? "NEW VERSION" : "CREATE"}</span></div>
            <form onSubmit={submit}>
              {revisionSource ? <div className="revision-context"><GitBranch size={16} /><span><strong>{revisionSource.song_title}</strong>V{revisionSource.version_no}를 기준으로 새 버전을 만듭니다.</span><button type="button" aria-label="수정 취소" onClick={resetForm}><X size={15} /></button></div> : null}
              <label className="field-label" htmlFor="prompt">음악의 시작이 될 아이디어 <span>필수</span></label>
              <div className="prompt-box"><textarea id="prompt" placeholder="비 오는 밤, 집으로 돌아가는 길에 듣고 싶은 따뜻한 인디 팝. 부드러운 여성 보컬과 위로가 되는 가사…" value={prompt} onChange={(event) => setPrompt(event.target.value)} minLength={10} maxLength={2000} required rows={5} /><div className="prompt-footer"><span><WandSparkles size={12} /> 장면, 감정, 악기를 자유롭게</span><span>{prompt.length}/2000</span></div></div>
              <div className="prompt-ideas"><span>이런 느낌은 어때요?</span>{examples.map((example) => <button key={example.label} type="button" onClick={() => { setPrompt(example.prompt); setGenre(example.genre); setMood(example.mood); }}>{example.label}<ArrowRight size={11} /></button>)}</div>
              <div className="form-divider" />
              <div className="field-row"><label className="field-label" htmlFor="title">트랙 이름 <span>선택</span></label><input id="title" placeholder="나만의 트랙에 이름을 붙여주세요" maxLength={100} value={title} onChange={(event) => setTitle(event.target.value)} /></div>
              <div className="field-grid">
                <SelectField id="genre" label="장르" value={genre} onChange={setGenre} options={["Indie pop", "Synth pop", "Acoustic pop", "R&B", "Lo-fi pop", "Rock"]} />
                <SelectField id="mood" label="분위기" value={mood} onChange={setMood} options={["Dreamy", "Nostalgic", "Uplifting", "Melancholic", "Energetic", "Peaceful"]} />
                <SelectField id="vocal" label="보컬" value={vocal} onChange={setVocal} options={["Soft female vocals", "Warm male vocals", "Airy female vocals", "Expressive male vocals"]} />
                <SelectField id="language" label="가사 언어" value={language} onChange={setLanguage} options={["Korean", "English", "Japanese"]} />
              </div>
              <div className="duration-row"><span className="field-label">길이</span><div className="segmented" role="group" aria-label="곡 길이">{[30, 60].map((length) => <button key={length} type="button" aria-pressed={seconds === length} onClick={() => setSeconds(length)}>{length}초</button>)}</div></div>
              <div className="lyrics-header"><span className="field-label">가사</span><div className="segmented" role="group" aria-label="가사 입력 방식"><button type="button" aria-pressed={lyricsMode === "auto"} onClick={() => setLyricsMode("auto")}><Sparkles size={12} /> 자동 생성</button><button type="button" aria-pressed={lyricsMode === "custom"} onClick={() => setLyricsMode("custom")}>직접 입력</button></div></div>
              {lyricsMode === "custom" ? <textarea className="custom-lyrics" aria-label="직접 입력 가사" placeholder={"[Verse]\n노래에 담고 싶은 가사를 적어주세요"} value={lyrics} onChange={(event) => setLyrics(event.target.value)} minLength={10} maxLength={3000} required rows={5} /> : <p className="field-help">입력한 아이디어에 맞춰 가사와 곡 구성을 함께 준비해요.</p>}
              <div className="provider-area"><label className="field-label" htmlFor="provider">생성 방식</label><div className="select-wrap"><select id="provider" value={provider} onChange={(event) => setProvider(event.target.value)}><option value="elevenlabs">ElevenLabs Music · 보컬 포함</option><option value="demo">로컬 데모 · 연주음 테스트</option></select><ChevronDown size={14} /></div>
                {provider === "demo" ? <p className="provider-note demo-note">데모는 저장·재생 확인용 합성 연주음입니다. AI 보컬이나 가사를 생성하지 않아요.</p> : config && !available ? <p className="provider-note">실제 노래를 생성하려면 서버의 .env에 ELEVENLABS_API_KEY를 설정해 주세요. 로컬 데모로 저장·재생 흐름을 먼저 확인할 수 있어요.</p> : <p className="provider-note">실제 AI 생성은 연결한 계정의 사용량이 소모됩니다.</p>}
              </div>
              {error ? <p className="form-error" role="alert">{error}</p> : null}
              <button className="button generate-button" type="submit" disabled={submitting || !available}>{submitting ? <LoaderCircle size={18} className="spin" /> : <AudioLines size={18} />}{submitting ? "제작 요청 중…" : revisionSource ? provider === "demo" ? "데모 수정본 만들기" : "새 버전 만들기" : provider === "demo" ? "데모 트랙 만들기" : "노래 만들기"}<ArrowRight size={17} className="button-arrow" /></button>
              <p className="submit-note"><Check size={12} /> 생성한 음악은 저장돼요. 유사도 검사는 별도로 요청하는 방식이에요.</p>
            </form>
          </section>

          <div className="output-column">
            <Player production={selected} />
            {selected?.status === "ready" ? <SimilarityPanel key={selected.id} production={selected} /> : null}
            {selected ? <VersionHistory versions={visibleVersions} selected={selected} onSelect={setSelectedId} onRevise={revise} /> : null}
            <SongLibrary songs={songs} selectedId={selectedSongId} onSelect={(song) => { setSelectedSongId(song.id); setSelectedId(song.latest_version.id); }} />
            <div className="workflow-note"><CircleHelp size={17} /><p><strong>원하는 버전에서 다시 시작하세요.</strong><span>각 버전의 음원·가사·설정을 보존해요.<br />검사 결과도 요청한 버전에 따로 남습니다.</span></p></div>
          </div>
        </div>
        <footer className="page-footer"><span>AUTOMUSIC STUDIO</span><span>아이디어에서 시작해, 나만의 소리로.</span></footer>
      </div>
    </main>
  </div>;
}

function SelectField({ id, label, value, onChange, options }: { id: string; label: string; value: string; onChange: (value: string) => void; options: string[] }) {
  return <div className="field-row"><label className="field-label" htmlFor={id}>{label}</label><div className="select-wrap"><select id={id} value={value} onChange={(event) => onChange(event.target.value)}>{options.map((option) => <option key={option}>{option}</option>)}</select><ChevronDown size={14} /></div></div>;
}

function Player({ production }: { production: Production | null }) {
  const busy = production ? isActive(production.status) : false;
  const error = production?.status === "failed" || production?.status === "needs_review";
  const ready = production?.status === "ready";
  return <section className="player-panel" aria-labelledby="player-heading">
    <div className="player-top"><span><Headphones size={14} /> LISTENING ROOM</span><span className="player-status">{production?.provider === "demo" ? "DEMO AUDIO" : ready ? "YOUR ORIGINAL" : "YOUR NEXT SOUND"}</span></div>
    <div className={`record-art ${busy ? "record-busy" : ""}`} aria-hidden="true"><div className="record-ring ring-outer" /><div className="record-ring ring-middle" /><div className="record-ring ring-inner" /><div className="record-label"><AudioLines size={35} strokeWidth={1.4} /><span>AM / 001</span></div><div className="record-orbit" /></div>
    <div className="player-copy"><span className="player-eyebrow">{busy ? "WORK IN PROGRESS" : ready ? production.provider === "demo" ? "LOCAL DEMO · NO AI VOCALS" : "READY TO PLAY" : "A LITTLE SPACE FOR YOUR SOUND"}</span><h2 id="player-heading">{production ? <span className="player-version">V{production.version_no}</span> : null}{production?.title ?? "아직 쓰이지 않은 멜로디"}</h2><p>{production ? `${production.genre} / ${production.mood} / ${duration(production.audio_duration ?? production.duration_seconds)}` : "첫 번째 아이디어를 음악으로 채워보세요."}</p></div>
    <div aria-live="polite" className="player-live">{busy && production ? <div className="generation-progress"><LoaderCircle size={16} className="spin" /><span>{statusLabel[production.status]}<small>창을 닫아도 워커가 계속 처리해요.</small></span></div> : error ? <p className="player-error" role="alert">{production.error_message}</p> : null}</div>
    {ready && production.audio_url ? <div className="audio-controls"><audio key={production.id} controls preload="metadata" src={production.audio_url} aria-label={`${production.title} 재생`} /><a className="download-button" href={production.download_url ?? "#"} download aria-label="음원 다운로드"><ArrowDownToLine size={18} /></a></div> : <div className="player-idle"><Disc3 size={19} /><span>{busy ? "음원을 준비하고 있어요" : error ? "입력이나 제공자 설정을 확인해 주세요" : "음악이 완성되면 여기서 바로 들을 수 있어요"}</span></div>}
    {ready && production.lyrics && production.provider !== "demo" ? <details className="lyrics-details"><summary>가사 보기 <ChevronDown size={14} /></summary><pre>{production.lyrics}</pre></details> : null}
  </section>;
}
