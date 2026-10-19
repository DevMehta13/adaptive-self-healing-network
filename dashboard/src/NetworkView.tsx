import { useMemo, useState } from 'react';
import { runs, viewAt } from './fixtures';
import { storyMoments } from './story';
import { NetworkScene } from './NetworkScene';

const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`;

export function NetworkView() {
  const [selected, setSelected] = useState(0);
  const [step, setStep] = useState(2);
  const run = runs[selected];
  const moments = useMemo(() => storyMoments(run), [run]);
  const moment = moments[step];
  const cursor = moment.cursor;
  const view = useMemo(() => viewAt(run, cursor), [run, cursor]);
  const changeRun = (index: number) => { setSelected(index); setStep(2); };

  return <section className="network-section" aria-labelledby="network-title">
    <div className="section-top"><div><span className="eyebrow">Recorded simulation · D1</span><h2 id="network-title">What the controller knew</h2><p>Explore saved evidence from Harshal’s backend. This viewer is independent of the live connection above.</p></div><span className="recorded-tag">Recorded run</span></div>
    <div className="playback-controls">
      <div className="run-tabs" role="group" aria-label="Recorded run">{runs.map((r, i) => <button key={r.name} type="button" aria-pressed={selected === i} onClick={() => changeRun(i)}>{r.name}</button>)}</div>
      <p>{run.description}</p>
      <div className="moment-nav"><button type="button" onClick={() => setStep(Math.max(0, step - 1))} disabled={step === 0}>Earlier moment</button><label htmlFor="story-moment">Jump to a moment</label><select id="story-moment" value={step} onChange={e => setStep(Number(e.target.value))}>{moments.map((item, index) => <option key={`${item.cursor}-${item.title}`} value={index}>{item.title}</option>)}</select><button type="button" onClick={() => setStep(Math.min(moments.length - 1, step + 1))} disabled={step === moments.length - 1}>Next moment</button></div>
      <div className="moment-current"><span>{seconds(view.clock.sim_ms)} into the recorded simulation</span><h3>{moment.title}</h3><p>{moment.explanation}</p></div>
      <p className="moment-help">The software uses smaller updates between these stops. You can follow the run using these moments alone.</p>
    </div>
    <NetworkScene view={view} timelineTitle="Story so far" timeline={<ol className="event-list">{moments.slice(0, step + 1).map((item, index) => <li className={item.source === 'injected' ? 'injected' : ''} key={`${item.cursor}-${item.title}`}><span className="story-time">{seconds(viewAt(run, item.cursor).clock.sim_ms)}</span><span>{item.title}</span>{item.source === 'injected' && <small>Presenter action</small>}{index === step && <small>Current moment</small>}</li>)}</ol>} />
  </section>;
}
