# widgets/transcript

Audio / video player + clickable transcript, bidirectionally syncs with media playback time:
- Media playback → current cue auto highlight + scroll into view
- click cue → media seek to cue.start + play

## Why encapsulate

When writing it themselves, agents need to solve three things:
1. Parse SRT/VTT into a `{start, end, text}` cue list (format not complex but easy to make mistakes with hours/minutes/seconds)
2. `timeupdate` event handler to find current cue (linear scan or binary search)
3. active cue auto scroll into view (`scrollIntoView({block: 'nearest'})`)

Each piece alone isn't hard, but together they are 50+ lines of JS; copying is more reliable.

## Cue format

A list of `{start: float, end: float, text: string}`, time unit seconds. Sources:
- WhisperX / faster-whisper transcription directly outputs this structure
- SRT: use [srt-parser-2](https://www.npmjs.com/package/srt-parser-2) or similar to convert
- VTT: use browser native `TextTrack` API then convert to cues

The agent parses it themselves then injects. The widget does not include a parser (parser is coupled to the specific source).

## Two versions

### HTML version (`transcript.html`)

Paste, replace `__CUES_JSON__` with the JSON literal of the cue array:

```python
import json
cues = [{'start': 0.0, 'end': 2.5, 'text': 'Hello'}, ...]
html = open('transcript.html').read().replace('__CUES_JSON__', json.dumps(cues))
```

For media file, use `<video src="video.mp4">` (change to `<audio>` for audio), serve in the same directory as transcript.html.

### React version (`Transcript.tsx`)

```tsx
import { Transcript, Cue } from './components/Transcript';

const cues: Cue[] = [{start: 0.0, end: 2.5, text: 'Hello'}, ...];
<Transcript mediaSrc="/video.mp4" cues={cues} kind="video" />
```

`kind="audio"` uses `<audio>` instead of `<video>`.

## Known limitations

- **Long transcript (10K+ cues)**: linear scan each timeupdate ~250ms fires once, for 10K cues it's microseconds level OK. For 100K+ cues, switch to binary search yourself (`useMemo` cues sorted by start, binary search current time).
- **Multi-speaker / speaker labels**: prefix cue text yourself like `[A]: hello`, the widget does not parse it.
- **Edit transcript**: currently read-only. For editable cues, write another widget (textarea per cue + save button).
