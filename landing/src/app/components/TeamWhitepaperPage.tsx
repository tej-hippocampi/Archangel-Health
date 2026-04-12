import { useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";

pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();

const PDF_SRC = "/team-whitepaper.pdf";
const MAX_PAGE_WIDTH = 920;
const MIN_PAGE_WIDTH = 280;

export default function TeamWhitepaperPage() {
  const containerRef = useRef<HTMLDivElement>(null);
  const [pageWidth, setPageWidth] = useState(800);
  const [numPages, setNumPages] = useState(0);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const update = () => {
      const w = el.getBoundingClientRect().width;
      setPageWidth(Math.max(MIN_PAGE_WIDTH, Math.min(MAX_PAGE_WIDTH, Math.floor(w))));
    };

    update();
    const ro = new ResizeObserver(() => update());
    ro.observe(el);
    window.addEventListener("resize", update);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", update);
    };
  }, []);

  return (
    <div className="whitepaper-page">
      <h1 className="whitepaper-sr-only">TEAM white paper</h1>

      <div ref={containerRef} className="whitepaper-scroll">
        <Document
          className="whitepaper-document"
          file={PDF_SRC}
          loading={<div className="whitepaper-message">Loading white paper…</div>}
          error={<div className="whitepaper-message whitepaper-error">Could not load the white paper.</div>}
          onLoadSuccess={(pdf) => setNumPages(pdf.numPages)}
          onLoadError={() => setNumPages(0)}
        >
          <div className="whitepaper-pages">
            {numPages > 0
              ? Array.from({ length: numPages }, (_, i) => (
                  <div key={i + 1} className="whitepaper-sheet">
                    <Page
                      pageNumber={i + 1}
                      width={pageWidth}
                      renderTextLayer={false}
                      renderAnnotationLayer={false}
                      loading={<div className="whitepaper-page-loading">Loading page {i + 1}…</div>}
                    />
                  </div>
                ))
              : null}
          </div>
        </Document>
      </div>

      <style>{`
        .whitepaper-page {
          min-height: calc(100vh - 3.5rem);
          background: #0a0a0b;
          color: #f5f5f7;
        }
        .whitepaper-sr-only {
          position: absolute;
          width: 1px;
          height: 1px;
          padding: 0;
          margin: -1px;
          overflow: hidden;
          clip: rect(0, 0, 0, 0);
          white-space: nowrap;
          border: 0;
        }
        .whitepaper-scroll {
          max-width: min(100%, ${MAX_PAGE_WIDTH + 48}px);
          margin: 0 auto;
          padding: 1.5rem 1rem 3rem;
        }
        @media (min-width: 640px) {
          .whitepaper-scroll {
            padding: 2rem 1.25rem 4rem;
          }
        }
        .whitepaper-document {
          display: flex;
          flex-direction: column;
          align-items: center;
        }
        .whitepaper-pages {
          display: flex;
          flex-direction: column;
          align-items: center;
          gap: 1.25rem;
          width: 100%;
        }
        .whitepaper-sheet {
          border-radius: 0.5rem;
          overflow: hidden;
          box-shadow:
            0 4px 6px rgba(0, 0, 0, 0.35),
            0 12px 24px rgba(0, 0, 0, 0.45),
            0 0 0 1px rgba(255, 255, 255, 0.06);
          background: #1a1a1c;
        }
        .whitepaper-sheet .react-pdf__Page__canvas {
          display: block;
        }
        .whitepaper-message {
          text-align: center;
          padding: 2rem 1rem;
          font-size: 0.9375rem;
          color: rgba(245, 245, 247, 0.85);
        }
        .whitepaper-error {
          color: #fda4af;
        }
        .whitepaper-page-loading {
          min-height: 12rem;
          display: flex;
          align-items: center;
          justify-content: center;
          padding: 2rem;
          font-size: 0.875rem;
          color: rgba(245, 245, 247, 0.65);
          background: #141418;
          border-radius: 0.5rem;
        }
      `}</style>
    </div>
  );
}
