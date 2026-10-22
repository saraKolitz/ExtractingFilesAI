// A random sample of the fields the model did NOT flag, added to the quick review.
//
// The calibration can only learn from fields a person has checked. If people check only what is
// flagged, the high scores are never tested and "the model is right 99% of the time" is never
// measured. Quality control does the same thing on a production line: pull a few good-looking
// items at random and look at them anyway.

export const SPOT_CHECK_RATE = 0.1;
// Always at least this many (or all of them, if there are fewer) so a small batch is still measured.
export const SPOT_CHECK_MIN = 3;

export interface Cell {
  rowKey: string;
  field: string;
}

export const cellId = (c: Cell) => `${c.rowKey}|${c.field}`;

// FNV-1a: a small, stable hash. The sample must not change when the screen re-renders, and must
// not depend on the order of the rows - so it is chosen by this hash, not by Math.random().
function hash(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h;
}

/** The ids of the cells to spot-check: the lowest-hash ceil(rate * n), but at least `min`. */
export function pickSpotChecks(cells: Cell[], rate = SPOT_CHECK_RATE, min = SPOT_CHECK_MIN): Set<string> {
  const count = Math.min(cells.length, Math.max(min, Math.ceil(cells.length * rate)));
  const ranked = cells.map((c) => ({ id: cellId(c), h: hash(cellId(c)) })).sort((a, b) => a.h - b.h || (a.id < b.id ? -1 : 1));
  return new Set(ranked.slice(0, count).map((c) => c.id));
}
