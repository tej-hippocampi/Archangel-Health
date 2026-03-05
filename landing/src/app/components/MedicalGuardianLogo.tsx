/**
 * Archangel Health - Medical Guardian Logo
 * Archangel sword / medical caduceus style
 */

interface MedicalGuardianLogoProps {
  className?: string;
  width?: number;
  height?: number;
  color?: string;
  accentColor?: string;
}

export default function MedicalGuardianLogo({
  className = "",
  width = 120,
  height = 120,
  color = "#f5f5f7",
  accentColor = "#00ffff",
}: MedicalGuardianLogoProps) {
  return (
    <svg
      width={width}
      height={height}
      viewBox="0 0 120 120"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
    >
      <g>
        <rect x="57" y="20" width="6" height="80" fill={accentColor} opacity="0.15" rx="3" />
        <rect x="58" y="20" width="4" height="80" fill={color} rx="2" />
        <rect x="58.5" y="22" width="1" height="76" fill={accentColor} opacity="0.4" rx="0.5" />
      </g>
      <g>
        <circle cx="60" cy="28" r="12" stroke={color} strokeWidth="1.5" fill="none" opacity="0.8" />
        <circle cx="60" cy="28" r="10" stroke={accentColor} strokeWidth="1" fill="none" opacity="0.3" />
        <circle cx="60" cy="28" r="4" fill={accentColor} opacity="0.5" />
        <circle cx="60" cy="28" r="2.5" fill={color} />
        <path d="M60 16 L62 24 L60 22 L58 24 Z" fill={color} opacity="0.9" />
        <path d="M50 22 L56 26 L54 24 L52 26 Z" fill={color} opacity="0.7" />
        <path d="M70 22 L64 26 L66 24 L68 26 Z" fill={color} opacity="0.7" />
      </g>
      <g opacity="0.9">
        <path d="M60 45 Q50 50, 48 58 Q46 66, 54 70" stroke={color} strokeWidth="2.5" fill="none" strokeLinecap="round" />
        <path d="M60 45 Q50 50, 48 58 Q46 66, 54 70" stroke={accentColor} strokeWidth="1.2" fill="none" strokeLinecap="round" opacity="0.4" />
        <circle cx="47" cy="58" r="3.5" fill={color} />
        <circle cx="47" cy="58" r="2" fill={accentColor} opacity="0.6" />
      </g>
      <g opacity="0.9">
        <path d="M60 55 Q70 60, 72 68 Q74 76, 66 80" stroke={color} strokeWidth="2.5" fill="none" strokeLinecap="round" />
        <path d="M60 55 Q70 60, 72 68 Q74 76, 66 80" stroke={accentColor} strokeWidth="1.2" fill="none" strokeLinecap="round" opacity="0.4" />
        <circle cx="73" cy="68" r="3.5" fill={color} />
        <circle cx="73" cy="68" r="2" fill={accentColor} opacity="0.6" />
      </g>
      <g>
        <path d="M60 100 L56 92 L64 92 Z" fill={color} />
        <path d="M60 100 L57 93 L63 93 Z" fill={accentColor} opacity="0.4" />
        <rect x="48" y="88" width="24" height="3" fill={color} rx="1.5" opacity="0.9" />
      </g>
      <g transform="translate(60, 60)">
        <rect x="-1.5" y="-8" width="3" height="16" fill={color} rx="1" opacity="0.9" />
        <rect x="-8" y="-1.5" width="16" height="3" fill={color} rx="1" opacity="0.9" />
        <circle cx="0" cy="0" r="2.5" fill={accentColor} opacity="0.5" />
        <circle cx="0" cy="0" r="1.5" fill={color} />
      </g>
    </svg>
  );
}
