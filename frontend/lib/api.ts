// The backend serves everything under /api. Locally it runs on its own port, so that is the
// default. On Vercel the browser reaches it on the same domain through the /api rewrite in
// vercel.json, so the frontend service is built with NEXT_PUBLIC_API_URL=/api (see its
// buildCommand there). Under `vercel dev` put NEXT_PUBLIC_API_URL=/api in frontend/.env.local.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api";

export interface PageInfo {
  file_id: string;
  file_name: string;
  page_number: number;
  image_url: string;
  thumbnail_url: string;
  width: number;
  height: number;
}

export interface UploadError {
  file_name: string;
  detail: string;
}

export interface UploadResponse {
  pages: PageInfo[];
  errors: UploadError[];
}

export async function uploadFiles(files: File[]): Promise<UploadResponse> {
  const form = new FormData();
  for (const file of files) form.append("files", file);
  let res: Response;
  try {
    res = await fetch(`${API_URL}/upload`, { method: "POST", body: form });
  } catch {
    throw new Error("לא ניתן להתחבר לשרת");
  }
  if (!res.ok) throw new Error(`שגיאת שרת (${res.status})`);
  return (await res.json()) as UploadResponse;
}

export async function extractPage(fileId: string, pageNumber: number): Promise<string> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/extract/${fileId}/${pageNumber}`, { method: "POST" });
  } catch {
    throw new Error("לא ניתן להתחבר לשרת");
  }
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
    const detail = typeof body?.detail === "string" ? body.detail : null;
    throw new Error(detail ? `החילוץ נכשל: ${detail}` : `שגיאת שרת (${res.status})`);
  }
  return ((await res.json()) as { text: string }).text;
}

/** One field's journey for the audit sheet: what the model extracted, what was saved, who vouched for it. */
export interface AuditEntry {
  field: string;
  original: string;
  final: string;
  confidence: number | null;
  status: string;
}

export interface ExportRow {
  values: Record<string, string>;
  link: string;
  /** The row number as shown in the table. */
  number: number;
  source: string;
  audit: AuditEntry[];
}

/** The counts of one working session's calibration - no values from any form. */
export interface CalibrationRunPayload {
  run_id: string;
  forms: number;
  reviewed: number;
  correct: number;
  ok_above: number;
  bands: { low: number; high: number; reviewed: number; correct: number }[];
  fields: { field: string; reviewed: number; correct: number }[];
  suggestion: Record<string, string | number>;
}

/** Saves this run's calibration; the server keeps the last five runs in a file. */
export async function saveCalibrationRun(run: CalibrationRunPayload): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/calibration/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(run),
    });
  } catch {
    throw new Error("לא ניתן להתחבר לשרת");
  }
  if (!res.ok) throw new Error(`שמירת נתוני הכיול נכשלה (${res.status})`);
}

/** Sends the (possibly hand-corrected) rows to the server and returns the generated .xlsx. */
export async function exportExcel(rows: ExportRow[]): Promise<Blob> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows }),
    });
  } catch {
    throw new Error("לא ניתן להתחבר לשרת");
  }
  if (!res.ok) throw new Error(`הייצוא נכשל (${res.status})`);
  return res.blob();
}
