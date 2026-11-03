import { AudioLines, GitBranch, LoaderCircle, PencilLine } from "lucide-react";
import { Production, Song, duration, isActive, statusLabel } from "@/lib/api";

export function SongLibrary({ songs, selectedId, onSelect }: {
  songs: Song[]; selectedId: string | null; onSelect: (song: Song) => void;
}) {
  return <section className="library panel" id="library" aria-labelledby="library-heading">
    <div className="panel-header"><h2 id="library-heading">내 곡 <span className="library-count">{songs.length}</span></h2><span className="micro-label">SONG LIBRARY</span></div>
    {songs.length === 0 ? <div className="library-empty"><AudioLines size={25} strokeWidth={1.3} /><p>첫 번째 트랙을 기다리고 있어요</p><span>같은 곡의 수정본을 한곳에 모아둘게요.</span></div> : <ul className="track-list">{songs.map((song) => <li key={song.id}>
      <button type="button" className={`track-row ${selectedId === song.id ? "selected" : ""}`} onClick={() => onSelect(song)} aria-pressed={selectedId === song.id} aria-label={`${song.title} · ${song.version_count}개 버전`}>
        <span className={`track-icon ${song.latest_version.provider === "demo" ? "demo" : ""}`}>{isActive(song.latest_version.status) ? <LoaderCircle size={18} className="spin" /> : <AudioLines size={18} />}</span>
        <span className="track-info"><strong>{song.title}</strong><span>{song.version_count}개 버전 · {song.latest_version.provider === "demo" ? "DEMO" : song.latest_version.genre}</span></span>
        <span className="version-badge">V{song.latest_version.version_no}</span>
      </button>
    </li>)}</ul>}
  </section>;
}

export function VersionHistory({ versions, selected, onSelect, onRevise }: {
  versions: Production[]; selected: Production; onSelect: (id: string) => void;
  onRevise: (version: Production) => void;
}) {
  return <section className="panel version-panel" aria-labelledby="versions-heading">
    <div className="panel-header"><h2 id="versions-heading"><GitBranch size={16} /> 버전 이력</h2><span className="micro-label">{versions.length} VERSIONS</span></div>
    <ul className="track-list">{versions.map((version) => {
      const parent = versions.find((item) => item.id === version.parent_production_id);
      return <li key={version.id} className="version-item">
        <button type="button" className={`track-row ${selected.id === version.id ? "selected" : ""}`} onClick={() => onSelect(version.id)} aria-pressed={selected.id === version.id} aria-label={`V${version.version_no} ${version.title} 선택`}>
          <span className="version-badge">V{version.version_no}</span>
          <span className="track-info"><strong>{version.title}</strong><span>{parent ? `V${parent.version_no}에서 수정` : "첫 버전"} · {duration(version.audio_duration ?? version.duration_seconds)} · {version.provider === "demo" ? "DEMO" : version.language}</span></span>
          <span className={`track-state state-${version.status}`}>{statusLabel[version.status]}</span>
        </button>
      </li>;
    })}</ul>
    <div className="version-actions"><button type="button" className="button secondary" onClick={() => onRevise(selected)} disabled={selected.status !== "ready"}><PencilLine size={15} /> V{selected.version_no} 기준으로 수정</button><p>이전 음원과 설정은 그대로 보관됩니다.</p></div>
    <details className="version-settings" key={selected.id}>
      <summary>V{selected.version_no} 가사·생성 설정 보기</summary>
      <dl><div><dt>프롬프트</dt><dd className="preserve-text">{selected.prompt}</dd></div>
        <div><dt>장르 · 분위기</dt><dd>{selected.genre} · {selected.mood}</dd></div>
        <div><dt>보컬 · 언어</dt><dd>{selected.vocal} · {selected.language}</dd></div>
        <div><dt>생성 방식</dt><dd>{selected.provider === "demo" ? "로컬 데모 · 합성 연주음" : selected.model_id}</dd></div>
        <div><dt>가사</dt><dd className="preserve-text">{selected.lyrics || (selected.provider === "demo" ? "데모는 가사를 생성하지 않습니다." : "가사가 아직 준비되지 않았습니다.")}</dd></div>
      </dl>
    </details>
  </section>;
}
