/**
 * A first-run checklist. Each step ticks itself off from real state rather
 * than from a click on the guide, so it can never claim something is done
 * that is not — and a user who skips ahead sees their progress anyway.
 */
export default function OnboardingGuide({ title, intro, steps, onDismiss }) {
  const done = steps.filter((s) => s.done).length

  return (
    <section className="card section onboarding" aria-labelledby="onboarding-heading">
      <div className="card-header">
        <div>
          <h2 id="onboarding-heading">{title}</h2>
          {intro && <p className="small muted onboarding-intro">{intro}</p>}
        </div>
        <span className="small muted" aria-live="polite">
          {done} of {steps.length} done
        </span>
      </div>
      <ol className="onboarding-steps">
        {steps.map((step) => (
          <li key={step.label} className={step.done ? 'done' : ''}>
            <span className="onboarding-check" aria-hidden="true">
              {step.done ? '✓' : ''}
            </span>
            <div>
              <strong>{step.label}</strong>
              {step.done && <span className="visually-hidden"> (done)</span>}
              {step.hint && <div className="small muted">{step.hint}</div>}
            </div>
          </li>
        ))}
      </ol>
      <div className="card-body onboarding-footer">
        <button type="button" className="secondary" onClick={onDismiss}>
          {done === steps.length ? 'Done — hide this guide' : 'Hide guide'}
        </button>
      </div>
    </section>
  )
}
