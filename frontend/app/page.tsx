"use client";

import { useCallback, useState } from "react";
import { CalibrationPanel } from "@/components/CalibrationPanel";
import { DataTable } from "@/components/DataTable";
import { ExportBar } from "@/components/ExportBar";
import { PageViewer } from "@/components/PageViewer";
import { ProgressPanel } from "@/components/ProgressPanel";
import { ReviewMode } from "@/components/ReviewMode";
import { Steps } from "@/components/Steps";
import { UploadZone } from "@/components/UploadZone";
import { useBatch } from "@/hooks/useBatch";
import type { PageInfo } from "@/lib/api";

export default function Home() {
  const batch = useBatch();
  const [viewing, setViewing] = useState<PageInfo | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const closeViewer = useCallback(() => setViewing(null), []);
  const closeReview = useCallback(() => setReviewing(false), []);

  return (
    <main className="container">
      <header className="page-header">
        <div className="brand">
          <span className="brand-logo" aria-hidden>
            ✚
          </span>
          <div>
            <h1>המעבדה המרכזית לנגיפים</h1>
            <p>חילוץ אוטומטי של נתונים מטפסי התחייבות</p>
          </div>
        </div>
        <Steps stats={batch.stats} />
      </header>

      <UploadZone busy={batch.phase === "uploading"} onFiles={(files) => void batch.addFiles(files)} />

      {batch.message && (
        <p className="error" role="alert">
          {batch.message}
        </p>
      )}
      {batch.uploadErrors.map((err, i) => (
        <p className="error" role="alert" key={i}>
          <span dir="ltr">{err.file_name}</span>: {err.detail}
        </p>
      ))}

      <ProgressPanel rows={batch.rows} phase={batch.phase} />

      {batch.rows.length > 0 && (
        <section className="panel" aria-label="טבלת בדיקה">
          <ExportBar stats={batch.stats} exporting={batch.exporting} onExport={() => void batch.exportRows()} onExtract={batch.extractAll}
            reviewCount={batch.reviewQueue.length}
            onReview={() => setReviewing(true)}
          />
          <div className="legend">
            <span className="legend-item review">לבדיקה: נקרא, אך אינו וודאי</span>
            <span className="legend-item unread">לא נקרא: השדה נשאר ריק</span>
            <span className="legend-item null">NULL: לא זוהה</span>
            <span className="legend-item conflict">כפילות / סתירה בין טפסים</span>
            <span className="muted">לחצו על תא כדי לתקן. תיקון ידני מסיר את הסימון.</span>
          </div>
          <DataTable
            rows={batch.rows}
            conflicts={batch.conflicts}
            onEdit={batch.editCell}
            onRetry={batch.retry}
            onRemove={batch.removeRow}
            onView={(row) => setViewing(row.page)}
          />
        </section>
      )}

      {batch.stats.done > 0 && <CalibrationPanel rows={batch.rows} />}

      {reviewing && (
        <ReviewMode
          rows={batch.rows}
          queue={batch.reviewQueue}
          conflicts={batch.conflicts}
          onEdit={batch.editCell}
          onConfirm={batch.confirmCell}
          onClose={closeReview}
        />
      )}
      {viewing && <PageViewer page={viewing} onClose={closeViewer} />}
    </main>
  );
}
