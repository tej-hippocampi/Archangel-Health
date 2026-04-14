import { useState } from "react";
import { Check, Copy, Mail } from "lucide-react";
import MedicalGuardianLogo from "@/app/components/MedicalGuardianLogo";

interface RecoveryResourcesEmailPreviewProps {
  userName?: string;
  clinicCode?: string;
  resourceCode?: string;
  hippocratesImageUrl?: string;
}

export default function RecoveryResourcesEmailPreview({
  userName = "Michael",
  clinicCode = "TG85PQXR",
  resourceCode = "1COGO60I",
  hippocratesImageUrl = "/hippocrates-email-bg.png",
}: RecoveryResourcesEmailPreviewProps) {
  const [copiedClinic, setCopiedClinic] = useState(false);
  const [copiedResource, setCopiedResource] = useState(false);
  const [imageAvailable, setImageAvailable] = useState(true);

  const copyToClipboard = async (text: string, type: "clinic" | "resource") => {
    try {
      await navigator.clipboard.writeText(text);
      if (type === "clinic") {
        setCopiedClinic(true);
        setTimeout(() => setCopiedClinic(false), 2000);
      } else {
        setCopiedResource(true);
        setTimeout(() => setCopiedResource(false), 2000);
      }
    } catch {
      // Preview-only utility: no-op if clipboard is unavailable.
    }
  };

  return (
    <div className="min-h-screen bg-[#EFF4FB] py-8 px-4 sm:py-12 sm:px-6 lg:px-8">
      <div className="mx-auto mb-6 max-w-2xl flex items-center justify-between">
        <h1 className="text-sm tracking-[0.14em] uppercase font-semibold text-gray-500">
          Recovery Resources Email Preview
        </h1>
        <a
          href="/"
          className="inline-flex items-center rounded-full border border-gray-300 px-4 py-1.5 text-sm text-gray-700 hover:bg-white transition-colors"
        >
          Back to landing
        </a>
      </div>

      <div className="max-w-2xl mx-auto">
        <div className="bg-white rounded-lg shadow-lg overflow-hidden">
          <div className="relative px-6 py-10 sm:px-10 sm:py-16 overflow-hidden">
            {imageAvailable ? (
              <div className="absolute inset-0">
                <img
                  src={hippocratesImageUrl}
                  alt="Classical medical painting"
                  className="w-full h-full object-cover"
                  onError={() => setImageAvailable(false)}
                />
                <div className="absolute inset-0 bg-gradient-to-b from-black/60 via-black/50 to-black/70" />
                <div className="absolute inset-0 bg-gradient-to-tr from-[#1D4ED8]/30 via-transparent to-[#2563EB]/20" />
              </div>
            ) : (
              <div className="absolute inset-0 bg-gradient-to-br from-[#0f172a] via-[#1e293b] to-[#1d4ed8]" />
            )}

            <div className="relative z-10">
              <div className="flex justify-center mb-8">
                <div className="bg-white/15 backdrop-blur-md rounded-2xl p-5 inline-block shadow-2xl border border-white/20">
                  <MedicalGuardianLogo width={90} height={90} color="#ffffff" accentColor="#93C5FD" />
                </div>
              </div>

              <h2
                className="text-4xl sm:text-5xl font-bold text-white text-center tracking-tight mb-6 drop-shadow-2xl"
                style={{ textShadow: "0 4px 20px rgba(0,0,0,0.5), 0 2px 8px rgba(0,0,0,0.3)" }}
              >
                Archangel Health
              </h2>

              <div className="flex items-center justify-center mb-6">
                <div className="h-px w-16 bg-gradient-to-r from-transparent via-white/60 to-transparent" />
                <div className="mx-3 w-1.5 h-1.5 rounded-full bg-white/80" />
                <div className="h-px w-16 bg-gradient-to-r from-transparent via-white/60 to-transparent" />
              </div>

              <div className="max-w-md mx-auto">
                <div className="bg-white/10 backdrop-blur-xl rounded-xl px-8 py-6 shadow-2xl border border-white/30">
                  <div className="flex items-center justify-center gap-2.5 mb-2">
                    <Mail className="w-5 h-5 text-blue-200" />
                    <p className="text-blue-100 text-sm font-medium tracking-wide uppercase">Your Care Package</p>
                  </div>
                  <p className="text-white text-xl sm:text-2xl font-semibold text-center leading-relaxed drop-shadow-lg">
                    Recovery Resources Ready
                  </p>
                </div>
              </div>
            </div>
          </div>

          <div className="px-6 py-8 sm:px-10 sm:py-10">
            <div className="mb-10">
              <p className="text-gray-800 text-lg sm:text-xl leading-relaxed font-light">
                Hi <span className="font-semibold text-gray-900">{userName}</span>,
              </p>
              <p className="text-gray-600 text-base sm:text-lg leading-relaxed mt-4 font-light">
                Your care team has prepared personalized recovery resources for you, including voice explanations and
                quick reference guides.
              </p>
            </div>

            <div className="flex items-center justify-center mb-10">
              <div className="h-px flex-1 bg-gradient-to-r from-transparent via-gray-300 to-transparent" />
              <div className="mx-4 w-1.5 h-1.5 rounded-full bg-amber-600" />
              <div className="h-px flex-1 bg-gradient-to-r from-transparent via-gray-300 to-transparent" />
            </div>

            <div className="mb-10">
              <div className="text-center mb-8">
                <h3 className="text-gray-900 font-semibold text-xs uppercase tracking-[0.2em] px-3">Your Access Codes</h3>
                <p className="text-gray-500 text-sm font-light italic mt-3">
                  Save these codes to access your personalized recovery plan
                </p>
              </div>

              <div className="mb-6">
                <label className="text-xs font-medium text-gray-500 uppercase tracking-[0.15em] mb-3 block text-center">
                  Health System Code
                </label>
                <div className="bg-gradient-to-br from-gray-50 to-gray-100/50 border border-gray-300 rounded-xl px-6 py-5">
                  <div className="flex items-center justify-between">
                    <p className="text-3xl sm:text-4xl font-mono font-bold text-gray-900 tracking-[0.15em]">{clinicCode}</p>
                    <button
                      onClick={() => copyToClipboard(clinicCode, "clinic")}
                      className="ml-6 flex items-center gap-2.5 px-5 py-2.5 bg-white/80 border border-gray-300 rounded-lg hover:bg-amber-50 hover:border-amber-700/50 transition-all duration-200"
                      aria-label="Copy health system code"
                    >
                      {copiedClinic ? (
                        <>
                          <Check className="w-4 h-4 text-green-700" />
                          <span className="text-sm font-medium text-green-700">Copied</span>
                        </>
                      ) : (
                        <>
                          <Copy className="w-4 h-4 text-gray-600" />
                          <span className="text-sm font-medium text-gray-600">Copy</span>
                        </>
                      )}
                    </button>
                  </div>
                </div>
              </div>

              <div className="mb-8">
                <label className="text-xs font-medium text-gray-500 uppercase tracking-[0.15em] mb-3 block text-center">
                  Resource Code
                </label>
                <div className="bg-gradient-to-br from-gray-50 to-gray-100/50 border border-gray-300 rounded-xl px-6 py-5">
                  <div className="flex items-center justify-between">
                    <p className="text-3xl sm:text-4xl font-mono font-bold text-gray-900 tracking-[0.15em]">{resourceCode}</p>
                    <button
                      onClick={() => copyToClipboard(resourceCode, "resource")}
                      className="ml-6 flex items-center gap-2.5 px-5 py-2.5 bg-white/80 border border-gray-300 rounded-lg hover:bg-amber-50 hover:border-amber-700/50 transition-all duration-200"
                      aria-label="Copy resource code"
                    >
                      {copiedResource ? (
                        <>
                          <Check className="w-4 h-4 text-green-700" />
                          <span className="text-sm font-medium text-green-700">Copied</span>
                        </>
                      ) : (
                        <>
                          <Copy className="w-4 h-4 text-gray-600" />
                          <span className="text-sm font-medium text-gray-600">Copy</span>
                        </>
                      )}
                    </button>
                  </div>
                </div>
              </div>
            </div>

            <button className="w-full relative overflow-hidden bg-gradient-to-r from-gray-800 via-gray-900 to-gray-800 text-white font-semibold py-5 px-6 rounded-xl shadow-xl mb-8">
              <span className="relative text-lg tracking-wide">View Your Recovery Plan</span>
            </button>

            <div className="relative overflow-hidden bg-gradient-to-br from-amber-50/50 to-gray-50 border border-amber-200/60 rounded-xl px-6 py-5 shadow-sm">
              <p className="relative text-gray-700 text-sm leading-relaxed text-center font-light">
                <span className="inline-block mr-2 text-amber-700">✦</span>
                <span className="font-medium text-gray-900">Pro tip:</span> Use your Health System Code and Resource Code above
                to access your plan. Best viewed on a computer or tablet for the full experience.
              </p>
            </div>
          </div>

          <div className="px-6 py-8 sm:px-10 bg-gradient-to-b from-gray-50 to-white border-t border-gray-200">
            <div className="flex items-center justify-center gap-3 mb-4">
              <MedicalGuardianLogo width={36} height={36} color="#4B5563" accentColor="#92400E" />
              <div className="h-8 w-px bg-gray-300" />
              <p className="text-gray-700 text-sm font-medium tracking-wide">Archangel Health</p>
            </div>
            <p className="text-gray-400 text-xs text-center font-light italic">Your personalized healthcare companion</p>
          </div>
        </div>
      </div>
    </div>
  );
}
