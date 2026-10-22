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

export const ID_LENGTH = 9;

/** The message to show when an ID number may not be saved, or null if it may. An empty one is fine. */
export function idLengthError(value: string): string | null {
  const text = value.trim();
  if (!text) return null;
  return /^\d{9}$/.test(text) ? null : `תעודת זהות חייבת להכיל בדיוק ${ID_LENGTH} ספרות`;
}

/** Rules that stop a value from being saved (typed into a cell, or exported), not just flagged. */
export const FIELD_VALIDATORS: Partial<Record<FieldKey, (value: string) => string | null>> = {
  id_number: idLengthError,
};

// Only a fallback for a response without "thresholds"; keep it equal to DEFAULT_CONF_EMPTY_BELOW
// in backend/app/services/ai_service.py.
const DEFAULT_EMPTY_BELOW = 15;
const DEFAULT_OK_ABOVE = 70;

// "review": read, but has to be checked (orange). "unread": scored under the "empty below"
// threshold, so nothing was really read (red). "ok": nothing to flag.
export type CellState = "ok" | "review" | "unread";

// [x0, y0, x1, y1] as fractions of the page: where a field was read (from the model's tight crops).
export type Box = [number, number, number, number];

// A number the model read that is a digit or two away from a barcode on the same page.
export interface BarcodeSuggestion {
  read: string;
  barcode: string;
}

export interface ParsedExtraction {
  values: FieldValues;
  /** The values as the model extracted them - kept so a later hand edit can be told apart. */
  original: FieldValues;
  /** The backend's CONF_OK_ABOVE: a score above it passes with no review. */
  okAbove: number;
  /** Every barcode decoded on the page - hints for the reviewer, never copied into a field. */
  barcodes: string[];
  /** Fields whose value a barcode confirmed (a second source). */
  barcodeConfirmed: FieldKey[];
  barcodeSuggestions: BarcodeSuggestion[];
  regions: Partial<Record<FieldKey, Box[]>>;
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
  const thresholds = (obj.thresholds ?? {}) as { empty_below?: unknown; ok_above?: unknown };
  const emptyBelow = typeof thresholds.empty_below === "number" ? thresholds.empty_below : DEFAULT_EMPTY_BELOW;

  const okAbove = typeof thresholds.ok_above === "number" ? thresholds.ok_above : DEFAULT_OK_ABOVE;
  const strings = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
  const suggestionsRaw = ((obj.barcode_suggestions ?? {}) as Record<string, unknown>).central_numbers;
  const barcodeSuggestions = (Array.isArray(suggestionsRaw) ? suggestionsRaw : []).filter(
    (s): s is BarcodeSuggestion =>
      !!s && typeof s === "object" && typeof (s as BarcodeSuggestion).read === "string" && typeof (s as BarcodeSuggestion).barcode === "string",
  );
  const regions: ParsedExtraction["regions"] = {};
  const regionsRaw = (obj.regions ?? {}) as Record<string, unknown>;
  const values = emptyValues();
  const confidence: ParsedExtraction["confidence"] = {};
  const states = {} as Record<FieldKey, CellState>;
  for (const { key } of FIELDS) {
    values[key] = asText(obj[key]);
    const score = confidenceRaw[key];
    if (typeof score === "number") confidence[key] = score;
    const boxes = regionsRaw[key];
    if (Array.isArray(boxes)) {
      const valid = boxes.filter(
        (b): b is Box => Array.isArray(b) && b.length === 4 && b.every((n) => typeof n === "number" && n >= 0 && n <= 1),
      );
      if (valid.length) regions[key] = valid;
    }
    const unread = typeof score === "number" && score < emptyBelow;
    states[key] = unread ? "unread" : flagged.has(key) ? "review" : "ok";
  }
  const barcodeConfirmed = strings(obj.barcode_confirmed).filter((k): k is FieldKey => FIELDS.some((f) => f.key === k));
  return { values, original: { ...values }, okAbove, regions, barcodes: strings(obj.barcodes), barcodeConfirmed, barcodeSuggestions, confidence, states };
}
