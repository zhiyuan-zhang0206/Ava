// Transcript.tsx — React transcript widget synced with <audio> / <video>.
//
// Usage:
//   import { Transcript } from './Transcript';
//   const cues = [{start: 0.0, end: 2.5, text: 'Hello'}, ...];
//   <Transcript mediaSrc="/video.mp4" cues={cues} />
//
// Active cue highlights + auto-scrolls into view as media plays. Click a
// cue to seek + play. Pass `kind="audio"` for audio-only files (default
// "video").

import { useEffect, useRef, useState } from 'react';

export interface Cue {
  start: number;  // seconds
  end: number;    // seconds
  text: string;
}

interface Props {
  mediaSrc: string;
  cues: Cue[];
  kind?: 'video' | 'audio';
  className?: string;
}

function fmtTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = (s - m * 60).toFixed(1);
  return `${m}:${sec.padStart(4, '0')}`;
}

export function Transcript({ mediaSrc, cues, kind = 'video', className }: Props) {
  const mediaRef = useRef<HTMLVideoElement | HTMLAudioElement | null>(null);
  const cueRefs = useRef<(HTMLDivElement | null)[]>([]);
  const [activeIdx, setActiveIdx] = useState<number>(-1);

  useEffect(() => {
    const m = mediaRef.current;
    if (!m) return;
    function onTimeUpdate() {
      const t = m!.currentTime;
      let idx = -1;
      for (let i = 0; i < cues.length; i++) {
        if (cues[i].start <= t && t < cues[i].end) { idx = i; break; }
      }
      setActiveIdx((prev) => {
        if (idx !== prev && idx >= 0) {
          cueRefs.current[idx]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }
        return idx;
      });
    }
    m.addEventListener('timeupdate', onTimeUpdate);
    return () => m.removeEventListener('timeupdate', onTimeUpdate);
  }, [cues]);

  function seek(start: number) {
    const m = mediaRef.current;
    if (!m) return;
    m.currentTime = start;
    void m.play();
  }

  const MediaTag = (kind === 'audio' ? 'audio' : 'video') as 'audio' | 'video';

  return (
    <div className={className} style={{ maxWidth: 900, margin: '1em auto', padding: '0 1em', fontFamily: 'system-ui, sans-serif' }}>
      <MediaTag
        ref={mediaRef as React.Ref<any>}
        controls
        style={{ width: '100%', maxHeight: '50vh' }}
        src={mediaSrc}
      />
      <div style={{ maxHeight: '60vh', overflowY: 'auto', border: '1px solid var(--border)', padding: '1em', borderRadius: 6, marginTop: '1em' }}>
        {cues.map((cue, i) => (
          <div
            key={i}
            ref={(el) => { cueRefs.current[i] = el; }}
            onClick={() => seek(cue.start)}
            style={{
              padding: '0.4em 0.5em',
              cursor: 'pointer',
              borderRadius: 4,
              background: i === activeIdx ? 'var(--active-cue)' : 'transparent',
              lineHeight: 1.4,
            }}
          >
            <span style={{ color: 'var(--text-muted)', fontSize: '0.85em', fontVariantNumeric: 'tabular-nums', marginRight: '0.6em' }}>
              {fmtTime(cue.start)}
            </span>
            {cue.text}
          </div>
        ))}
      </div>
    </div>
  );
}
