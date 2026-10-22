// The extracted fields, in the order of the data model (CLAUDE.md section 4).
export const FIELDS = [
  { key: "first_name", label: "שם פרטי" },
  { key: "last_name", label: "שם משפחה" },
  { key: "id_number", label: "תעודת זהות" },
  { key: "central_numbers", label: "מספר מרכזי" },
  { key: "received_date", label: "תאריך קבלה" },
  { key: "sending_entity", label: "גורם שולח" },
  { key: "commitment_numbers", label: "מספר התחייבות" },
] as const;

export type FieldKey = (typeof FIELDS)[number]["key"];
export type FieldValues = Record<FieldKey, string>;

export const emptyValues = (): FieldValues =>
  Object.fromEntries(FIELDS.map((f) => [f.key, ""])) as FieldValues;

// Only a fallback for a response without "thresholds"; keep it equal to DEFAULT_CONF_EMPTY_BELOW
// in backend/app/services/ai_service.py.
const DEFAULT_EMPTY_BELOW = 20;

// "review": read, but has to be checked (orange). "unread": scored under the "empty below"
// threshold, so nothing was really read (red). "ok": nothing to flag.
export type CellState = "ok" | "review" | "unread";

export interface ParsedExtraction {
  values: FieldValues;
  confidence: Partial<Record<FieldKey, number>>;
  states: Record<FieldKey, CellState>;
}

const asText = (value: unknown) =>
  Array.isArray(value) ? value.join(", ") : typeof value === "string" ? value : "";

/** Turns the backend's JSON text into table values. Returns null if it is not a JSON object. */
export function parseExtraction(text: string): ParsedExtraction | null {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return null;
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const obj = data as Record<string, unknown>;

  const flagged = new Set(Array.isArray(obj.fields_requiring_review) ? obj.fields_requiring_review : []);
  const confidenceRaw = (obj.confidence ?? {}) as Record<string, unknown>;
  // The backend sends its own thresholds so the colours follow .env, not a copy of them here.
  const thresholds = (obj.thresholds ?? {}) as { empty_below?: unknown };
  const emptyBelow = typeof thresholds.empty_below === "number" ? thresholds.empty_below : DEFAULT_EMPTY_BELOW;

  const values = emptyValues();
  const confidence: ParsedExtraction["confidence"] = {};
  const states = {} as Record<FieldKey, CellState>;
  for (const { key } of FIELDS) {
    values[key] = asText(obj[key]);
    const score = confidenceRaw[key];
    if (typeof score === "number") confidence[key] = score;
    const unread = typeof score === "number" && score < emptyBelow;
    states[key] = unread ? "unread" : flagged.has(key) ? "review" : "ok";
  }
  return { values, confidence, states };
}
