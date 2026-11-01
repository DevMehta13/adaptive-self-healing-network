import { useMemo, useState } from 'react';
import { runs, viewAt } from './fixtures';
import { elapsedText, formatNumber, lateLabel, pathText, unitOf } from './format';
import { NetworkGraph, RoutePanel, TimelineRows } from './NetworkPanels';
import { CountsGrid, StaleNotice } from './NetworkScene';
import { storyMoments, storyProgress, storyRows } from './story';

/** The saved-runs story: the same screens as the live pages, driven by a recording, so it works with the backend off. */
export function NetworkView() {
  const [selected, setSelected] = useState(0);
  const [step, setStep] = useState(2);
  const run = runs[selected];
  const moments = useMemo(() => storyMoments(run), [run]);
  const moment = moments[step];
  const view = useMemo(() => viewAt(run, moment.cursor), [run, moment]);
  const progress = storyProgress(step, moments.length);
  const unit = unitOf(view.mode);
  const rows = useMemo(() => storyRows(moments, step, m => viewAt(run, m.cursor).clock.sim_ms), [run, moments, step]);
  const changeRun = (index: number) => { setSelected(index); setStep(2); };
  const readings = [...view.readings.recent].reverse().slice(0, 5);

  return <>
    <section className="detail-panel run-panel" aria-labelledby="run-title">
      <div className="run-top"><h2 id="run-title" className="section-title">Choose a recording</h2><span className="recorded-tag">Recorded run · works offline</span></div>
      <div className="segmented" role="group" aria-label="Recorded run">{runs.map((r, i) => <button key={r.name} type="button" aria-pressed={selected === i} onClick={() => changeRun(i)}>{r.name}</button>)}</div>
      <p className="run-description">{run.description}</p>
      <div className="story-nav">
        <button type="button" className="plain-button" onClick={() => setStep(Math.max(0, step - 1))} disabled={step === 0}>← Previous moment</button>
        <div className="story-where">
          <strong>{progress.label}</strong>
          <div className="story-bar" role="progressbar" aria-label="Position in the story" aria-valuemin={1} aria-valuemax={progress.total} aria-valuenow={progress.position}><span style={{ width: `${progress.percent}%` }} /></div>
          <ol className="story-dots" aria-label="Moments">{moments.map((m, i) => <li key={`${m.cursor}-${m.title}`}><button type="button" className={i === step ? 'here' : i < step ? 'seen' : ''} aria-label={`Moment ${i + 1}: ${m.title}`} aria-current={i === step ? 'step' : undefined} title={m.title} onClick={() => setStep(i)} /></li>)}</ol>
        </div>
        <button type="button" className="plain-button" onClick={() => setStep(Math.min(moments.length - 1, step + 1))} disabled={step === moments.length - 1}>Next moment →</button>
      </div>
      <div className="story-jump"><label htmlFor="story-moment">Jump to a moment</label><select id="story-moment" value={step} onChange={e => setStep(Number(e.target.value))}>{moments.map((item, index) => <option key={`${item.cursor}-${item.title}`} value={index}>{index + 1}. {item.title}</option>)}</select></div>
      <div className="moment-current" aria-live="polite"><span>{elapsedText(view.clock.sim_ms)} into the recording</span><h3>{moment.title}</h3><p>{moment.explanation}</p></div>
      <p className="moment-help">The software uses smaller updates between these stops. You can follow the run using these moments alone.</p>
    </section>
    <StaleNotice view={view} />
    <div className="network-layout"><NetworkGraph view={view} /><RoutePanel view={view} unit={unit} /></div>
    <CountsGrid view={view} />
    <div className="details-grid">
      <section className="detail-panel readings-table" aria-labelledby="run-readings"><h3 id="run-readings">Latest readings at this moment</h3>
        {readings.length ? <div className="table-scroll"><table>
          <thead><tr><th scope="col">Time at C</th><th scope="col">Value</th><th scope="col">Path observed at C</th><th scope="col">Live or replayed</th></tr></thead>
          <tbody>{readings.map(r => <tr key={`${r.origin}-${r.session}-${r.seq}`} className={r.replayed ? 'replayed-row' : ''}>
            <td>{elapsedText(r.received_ms)}</td><td>{formatNumber(r.value, unit)} {unit}</td><td>{pathText(r.observed_path)}</td><td>{r.replayed ? lateLabel(r.delay_ms) : 'Live'}</td></tr>)}</tbody></table></div>
          : <p className="empty-state">No reading has reached C yet at this moment.</p>}
      </section>
      <section className="detail-panel timeline" aria-labelledby="run-story"><h3 id="run-story">Story so far</h3>
        <TimelineRows items={rows} currentKey={rows.at(-1)?.key} />
      </section>
    </div>
  </>;
}
