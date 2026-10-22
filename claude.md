# Automatic Data Extraction System for Commitment Forms

This document is based on the project's technical specification. It defines what we build, in what order, and how we work on it with Claude Code.

## 1. Overview and Goal

The system replaces manual typing of medical commitment forms (printed, scanned or handwritten) into an Excel file.
It provides an end-to-end solution: file upload, automatic processing, human-in-the-loop review with flagging of fields that need checking, and structured data export.

The project is a challenge from the Central Virology Laboratory (Ministry of Health) in the AI Open Source Hackathon 2026.
All forms in the samples are dummy forms: the personal details in them were changed.

## 2. Data Flow

**Stage A - Ingestion and storage**
- The user uploads PDF files (sometimes with many pages) or images through the user interface.
- The server stores the original files in internal storage, to give each file a fixed and stable access path.
- PDF files are split into single pages and converted to high-resolution images (PyMuPDF).

**Stage B - Image pre-processing**
- OpenCV removes noise, straightens skewed scans (deskew) and raises contrast to improve readability.
- The image is kept in color, with no conversion to black and white. The original color image is kept for display on screen.

**Stage C - Optical recognition (Cloud OCR)**
- The processed images are sent to a cloud OCR service (Google Cloud Vision), which excels at decoding printed and handwritten Hebrew.
- The OCR returns the raw text and the optical confidence scores.

**Stage D - Logical analysis and field extraction (LLM)**
- The OCR text is passed in a structured prompt to an advanced cloud language model, through the API.
- The model analyzes the context, corrects common optical errors (see section 6), and extracts the required fields into a structured JSON object, with a confidence score for each field.

**Stage E - Validation and control**
- Dedicated code functions (Python) run hard validity checks, such as ID number length and date validity.
- Fields that failed validation or received a low score are flagged automatically (see section 5).

**Stage F - User interface and export**
- The data is shown in a consolidated table. Suspicious fields are highlighted in orange for the lab technician to check and fix.
- Every row includes a direct link to view the original file.
- After final approval, the system generates and downloads a structured Excel file.

## 3. Technologies

| Area | Choice |
| --- | --- |
| Frontend | Next.js (App Router), React, TypeScript. Hebrew right-to-left (RTL) interface |
| Backend | Python (FastAPI) |
| Image processing | OpenCV, PyMuPDF (fitz) |
| OCR | Google Cloud Vision API |
| LLM | API calls to a flagship model (for example GPT-4o, Claude or Gemini). The model name is set in `.env` |
| Data and export | Pandas and openpyxl |
| Storage | SQLite + a local file folder (to start) |

Settings and secrets are kept in `.env` only (API keys, model name, Google credentials). They are never hardcoded and never pushed to git.

## 4. Data Model

Each row in the Excel file represents one commitment. The fields:

1. First name
2. Last name
3. ID number (stored as text)
4. Central number: a number on a sticker labeled "מרכזי". If there are several, separate them with commas and keep all of them
5. Sample received date: from the "התקבל" (received) stamp (or the handwriting under the central number)
6. Sending party: an HMO or a hospital, from a closed list (Clalit, Maccabi, Meuhedet, Leumit, and the hospitals in Israel). The model picks from the list and does not guess
7. Commitment number: including other labels such as "דרישה" (request) or "דגימה" (sample) (in Leumit), keeping several numbers in the same row
8. A dynamic link to the original document

A field that is not found stays empty. A guessed value is never filled in.

## 5. Confidence Scores and Flagging

Confidence is set for each field separately (0 to 100%):

- **Below 15%:** the field stays empty and flagged.
- **Between 15% and 90%:** the field is filled and highlighted in orange (not sure, please check).
- **Above 90%:** the field is filled with no highlighting.

The thresholds are stored in settings (`CONF_EMPTY_BELOW=15`, `CONF_OK_ABOVE=90`) so they can be tuned.
A field that fails a hard validity check is flagged in orange even if its score is high.

## 6. OCR Error Correction

The correction deals with known character confusions, for example the Hebrew letter ז recognized as 7, or S recognized as 5.

- Corrections apply to digit-only fields (ID number, central number, commitment number, dates).
- Both the raw value and the corrected value are stored.
- A field that was corrected automatically is flagged, unless another source confirms it.
- It is forbidden to freely "fix" a name, ID number or any number based on what "looks reasonable".

## 7. Development Strategy (MVP)

Submission is in one week (21/10). First we build a demo that works on simple forms, then we improve.

**Stage A - Core:** a full end-to-end pipeline for standard forms (one commitment per page): upload, conversion to image, OCR, LLM extraction, display in a table, and export to Excel.

**Stage B - Reliability:** validity checks in code and a mechanism for highlighting suspicious fields.

**Stage C - Improvements, only if there is time:** complex edge cases, such as several commitments on the same scanned page and difficult handwriting.

### Work order (one step at a time)

1. Project skeleton (`backend/` and `frontend/`), file upload, PDF-to-image conversion and displaying the images.
2. Image pre-processing with OpenCV.
3. OCR with Google Cloud Vision, storing the text.
4. Field extraction by the LLM from the OCR text into a JSON object. Test on 5 simple printed forms.
5. Validation and confidence scores.
6. Table on screen (Next.js): orange fields, in-place editing, link to the original page.
7. Export to Excel.
8. (Only if time remains) Decode the barcode of the central number and add it as another source for the confidence score.

After each step: run it, check that it works and look at the result, and only then continue.

## 8. Project Structure

```
backend/    FastAPI server, image processing, OCR, LLM, validation, export
frontend/   Next.js + React + TypeScript
samples/    The sample files (PDF, dummy forms)
docs/       Specification documents
.env        Settings and secrets (not in git)
```

## 9. Working Rules with Claude Code

- Work in small steps. After each step: how to run it and what to check.
- Ask before big decisions: a new library, a structure change or a stack change.
- Do not invent data. Test only on the files in `samples/`.
- No personal information in logs.

## 10. Decisions Made

1. **What the LLM receives:** as in the specification, the LLM receives the text the OCR extracted (including word positions if the OCR returns them), and extracts the relevant fields according to context. Also sending the page image to the model remains a possible improvement later, and is not done in the first stage.
2. **ID number length:** the length check stays as a hard check. In the samples almost all ID numbers are 11223344 (8 digits), so they will be flagged in orange, and that is fine; there is no need to work around it.
3. **Barcodes:** not now. Barcode decoding of the central number is added only at the end (step 8), if time remains.
4. **Image color:** keep the image in color. Do not convert to black and white, so that blue pen, faint stamps and yellow highlighter are not erased. Noise removal, straightening and contrast are done on the copy sent to the OCR, and the original color image is kept for display on screen.