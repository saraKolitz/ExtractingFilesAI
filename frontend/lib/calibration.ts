import type { FieldKey, FieldValues } from "./fields";

// Every time a technician confirms or corrects a field, that is a fact: the model was right or
// wrong about it, and it had given the field a confidence score. Put together, those facts say how
// far the scores can be trusted - and where the "no review needed" line could sit. This only
// measures and recommends; changing CONF_OK_ABOVE stays a human decision.

export type Outcome = "typed" | "confirmed" | "unreviewed";

/** The part of a table row the calibration needs (the hook's Row satisfies it). */
export interface CalibrationRow {
  status: string;
  values: FieldValues;
  /** What the model extracted, before any hand edit. */
  original: FieldValues;
  confidence: Partial<Record<FieldKey, number>>;
  edited: Partial<Record<FieldKey, true>>;
  /** The backend's CONF_OK_ABOVE: a score above it passes with no review. */
  okAbove: number;
}

/** What a human did with a field: left it unchecked, confirmed the model's value, or changed it. */
export function fieldOutcome(row: CalibrationRow, key: FieldKey): Outcome {
  if (!row.edited[key]) return "unreviewed";
  return row.values[key].trim() === row.original[key].trim() ? "confirmed" : "typed";
}

export const MIN_SAMPLES = 10;
export const MIN_TAIL = 5;
export const TARGET_ACCURACY = 0.98;

export const BANDS: { from: number; to: number }[] = [
  { from: 0, to: 9 },
  { from: 10, to: 49 },
  { from: 50, to: 69 },
  { from: 70, to: 89 },
  { from: 90, to: 100 },
];

interface Sample {
  field: FieldKey;
  score: number;
  correct: boolean;
}

export interface BandStat {
  from: number;
  to: number;
  reviewed: number;
  correct: number;
  accuracy: number | null;
}

export interface FieldStat {
  field: FieldKey;
  reviewed: number;
  correct: number;
}

export type Suggestion =
  | { kind: "need-data"; have: number; need: number }
  | { kind: "none" }
  | { kind: "keep"; cutoff: number; accuracy: number; count: number }
  /** Scores from `cutoff` up were right often enough: the clean line could drop, saving `saved` reviews. */
  | { kind: "lower"; cutoff: number; accuracy: number; count: number; saved: number; current: number }
  /** The current clean line is too low: scores below `cutoff` are not accurate enough. */
  | { kind: "raise"; cutoff: number; accuracy: number; count: number; current: number };

export interface Calibration {
  reviewed: number;
  correct: number;
  accuracy: number | null;
  bands: BandStat[];
  fields: FieldStat[];
  suggestion: Suggestion;
}

const ratio = (a: number, b: number) => (b === 0 ? null : a / b);

function collect(rows: CalibrationRow[], fields: readonly FieldKey[]): Sample[] {
  const samples: Sample[] = [];
  for (const row of rows) {
    if (row.status !== "done") continue;
    for (const field of fields) {
      const score = row.confidence[field];
      const outcome = fieldOutcome(row, field);
      if (typeof score === "number" && outcome !== "unreviewed") {
        samples.push({ field, score, correct: outcome === "confirmed" });
      }
    }
  }
  return samples;
}

function suggest(samples: Sample[], rows: CalibrationRow[], fields: readonly FieldKey[]): Suggestion {
  if (samples.length < MIN_SAMPLES) return { kind: "need-data", have: samples.length, need: MIN_SAMPLES };
  const done = rows.filter((r) => r.status === "done");
  const okAbove = done[0]?.okAbove ?? 70;
  const current = okAbove + 1; // the lowest score that passes with no review today

  // The lowest cutoff from which up the model was right often enough, on enough reviewed fields.
  let best: { cutoff: number; accuracy: number; count: number } | null = null;
  for (const cutoff of [...new Set(samples.map((s) => s.score))].sort((a, b) => a - b)) {
    const tail = samples.filter((s) => s.score >= cutoff);
    const accuracy = tail.filter((s) => s.correct).length / tail.length;
    if (tail.length >= MIN_TAIL && accuracy >= TARGET_ACCURACY) {
      best = { cutoff, accuracy, count: tail.length };
      break;
    }
  }
  if (!best) return { kind: "none" };
  if (best.cutoff > current) return { kind: "raise", ...best, current };
  if (best.cutoff === current) return { kind: "keep", ...best };
  // Fields that are flagged today only because of their score, and would pass at the new line.
  let saved = 0;
  for (const row of done) {
    for (const field of fields) {
      const score = row.confidence[field];
      if (typeof score === "number" && score >= best.cutoff && score <= okAbove && row.original[field].trim()) saved += 1;
    }
  }
  return { kind: "lower", ...best, saved, current };
}

/** Accuracy of the model's scores, from the fields a human has checked so far. */
export function calibrate(rows: CalibrationRow[], fields: readonly FieldKey[]): Calibration {
  const samples = collect(rows, fields);
  const correct = samples.filter((s) => s.correct).length;
  const bands = BANDS.map(({ from, to }) => {
    const inBand = samples.filter((s) => s.score >= from && s.score <= to);
    const ok = inBand.filter((s) => s.correct).length;
    return { from, to, reviewed: inBand.length, correct: ok, accuracy: ratio(ok, inBand.length) };
  });
  return {
    reviewed: samples.length,
    correct,
    accuracy: ratio(correct, samples.length),
    bands,
    fields: fields.map((field) => {
      const mine = samples.filter((s) => s.field === field);
      return { field, reviewed: mine.length, correct: mine.filter((s) => s.correct).length };
    }),
    suggestion: suggest(samples, rows, fields),
  };
}
