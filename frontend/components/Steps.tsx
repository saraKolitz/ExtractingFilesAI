interface Props {
  stats: { total: number; done: number; awaiting: number; pending: number };
}

const STEPS = ["העלאה", "חילוץ", "בדיקה ותיקון", "ייצוא"];

// The current step follows the state of the batch: nothing yet -> upload; pages waiting or being
// read -> extraction; everything read -> review.
function currentStep({ total, done, awaiting, pending }: Props["stats"]) {
  if (total === 0) return 0;
  if (awaiting > 0 || pending > 0) return 1;
  return done > 0 ? 2 : 1;
}

export function Steps({ stats }: Props) {
  const current = currentStep(stats);
  return (
    <ol className="steps" aria-label="שלבי העבודה">
      {STEPS.map((label, i) => (
        <li key={label} className={`step${i < current ? " past" : ""}${i === current ? " current" : ""}`}>
          <span className="step-dot">{i < current ? "✓" : i + 1}</span>
          <span className="step-label">{label}</span>
        </li>
      ))}
    </ol>
  );
}
