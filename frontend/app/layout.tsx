import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "חילוץ נתונים מטפסי התחייבות",
  description: "העלאת טפסי התחייבות והצגת העמודים",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="he" dir="rtl">
      <body>{children}</body>
    </html>
  );
}
