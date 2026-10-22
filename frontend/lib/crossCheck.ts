import type { FieldKey, FieldValues } from "./fields";

// Advisory checks across the rows of one batch. They only point at cells - nothing is changed or
// dropped, because two pages can legitimately share a value (two pages of one form).
export type ConflictKind = "duplicate" | "conflict";
export interface Conflict {
  kind: ConflictKind;
  text: string;
}
export type RowConflicts = Partial<Record<FieldKey, Conflict>>;

interface Input {
  key: string;
  values: FieldValues;
}

const norm = (s: string) => s.trim().replace(/\s+/g, " ").toLowerCase();
const tokens = (s: string) => s.split(",").map((t) => t.trim()).filter(Boolean);

// Mirrors validate_id_number in backend/app/services/validation.py: 9 digits + Israeli check digit.
// Only a plausible ID is compared across rows - a misread ID matching another row means nothing.
function isPlausibleId(id: string) {
  if (!/^\d{9}$/.test(id)) return false;
  let total = 0;
  for (let i = 0; i < 9; i++) {
    const n = Number(id[i]) * (i % 2 === 0 ? 1 : 2);
    total += n > 9 ? n - 9 : n;
  }
  return total % 10 === 0;
}

/** Groups row numbers (1-based, as shown in the table) by a key. Rows without a key are skipped. */
function groupBy(rows: Input[], keysOf: (r: Input) => string[]) {
  const groups = new Map<string, number[]>();
  rows.forEach((r, i) => {
    for (const k of new Set(keysOf(r))) groups.set(k, [...(groups.get(k) ?? []), i + 1]);
  });
  return groups;
}

const rowsText = (nums: number[]) => (nums.length === 1 ? `שורה ${nums[0]}` : `שורות ${nums.join(", ")}`);

/** Finds duplicates and contradictions. The result is keyed by row key, then by field. */
export function findConflicts(rows: Input[]): Record<string, RowConflicts> {
  const out: Record<string, RowConflicts> = {};
  const add = (rowNum: number, field: FieldKey, kind: ConflictKind, text: string) => {
    const row = rows[rowNum - 1];
    // The first finding for a cell wins, so a contradiction is not hidden by a plain duplicate.
    (out[row.key] ??= {})[field] ??= { kind, text };
  };
  const others = (nums: number[], self: number) => nums.filter((n) => n !== self);
  const id = (r: Input) => r.values.id_number.trim();

  // 1. The same central number on more than one row: a duplicate, or - if the two rows name
  //    different patients - a contradiction.
  for (const [num, nums] of groupBy(rows, (r) => tokens(r.values.central_numbers))) {
    if (nums.length < 2) continue;
    for (const n of nums) {
      const rest = others(nums, n);
      const sameSample = rest.filter((o) => {
        const a = id(rows[n - 1]);
        const b = id(rows[o - 1]);
        return !(isPlausibleId(a) && isPlausibleId(b) && a !== b);
      });
      const clash = rest.filter((o) => !sameSample.includes(o));
      if (clash.length) add(n, "central_numbers", "conflict", `מספר מרכזי ${num} מופיע גם ב${rowsText(clash)} עם תעודת זהות אחרת`);
      if (sameSample.length) add(n, "central_numbers", "duplicate", `מספר מרכזי ${num} מופיע גם ב${rowsText(sameSample)}`);
    }
  }

  // 2. The same commitment number from the same sender. The same number from two different
  //    senders is two different commitments, so the sender has to be known and equal.
  const commitmentKeys = (r: Input) =>
    r.values.sending_entity.trim() ? tokens(r.values.commitment_numbers).map((t) => `${r.values.sending_entity.trim()}|${t}`) : [];
  for (const [k, nums] of groupBy(rows, commitmentKeys)) {
    if (nums.length < 2) continue;
    const num = k.split("|").slice(1).join("|");
    for (const n of nums) add(n, "commitment_numbers", "duplicate", `התחייבות ${num} מופיעה גם ב${rowsText(others(nums, n))}`);
  }

  // 3. The same (plausible) ID with a different name.
  for (const [pid, nums] of groupBy(rows, (r) => (isPlausibleId(id(r)) ? [id(r)] : []))) {
    if (nums.length < 2) continue;
    for (const field of ["first_name", "last_name"] as const) {
      for (const n of nums) {
        const mine = norm(rows[n - 1].values[field]);
        const differing = others(nums, n).filter((o) => {
          const theirs = norm(rows[o - 1].values[field]);
          return mine && theirs && mine !== theirs;
        });
        if (differing.length) add(n, field, "conflict", `אותה תעודת זהות (${pid}) ב${rowsText(differing)} עם ${field === "first_name" ? "שם פרטי" : "שם משפחה"} אחר`);
      }
    }
  }

  // 4. The same full name with a different (plausible) ID.
  const fullName = (r: Input) => (r.values.first_name.trim() && r.values.last_name.trim() ? [norm(`${r.values.first_name} ${r.values.last_name}`)] : []);
  for (const nums of groupBy(rows, fullName).values()) {
    for (const n of nums) {
      const mine = id(rows[n - 1]);
      if (!isPlausibleId(mine)) continue;
      const differing = others(nums, n).filter((o) => {
        const theirs = id(rows[o - 1]);
        return isPlausibleId(theirs) && theirs !== mine;
      });
      if (differing.length) add(n, "id_number", "conflict", `אותו שם ב${rowsText(differing)} עם תעודת זהות אחרת`);
    }
  }
  return out;
}
