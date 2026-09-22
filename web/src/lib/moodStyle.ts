import {
  Activity,
  CloudRain,
  Crown,
  Disc3,
  Flame,
  Heart,
  Leaf,
  Moon,
  Music2,
  Sparkles,
  Sun,
  Zap,
  type LucideIcon,
} from "lucide-react";

export type MoodLook = { icon: LucideIcon; bg: string };

// Правила по подстроке (рус + англ, т.к. настроения едут и из AI по тексту, и из vibe-детектора).
const RULES: [RegExp, MoodLook][] = [
  [/т[её]мн|dark|noir/, { icon: Moon, bg: "linear-gradient(135deg,#334155,#0f172a)" }],
  [/меланхол|melanchol|груст|sad|печал|тоск/, { icon: CloudRain, bg: "linear-gradient(135deg,#64748b,#475569)" }],
  [/танце|dance|party|клуб|весел|groov/, { icon: Disc3, bg: "linear-gradient(135deg,#ec4899,#f97316)" }],
  [/энерг|energet|driv|мощн|бодр|power/, { icon: Zap, bg: "linear-gradient(135deg,#f59e0b,#ef4444)" }],
  [/агресс|aggress|зло|angry|ярост|fierce/, { icon: Flame, bg: "linear-gradient(135deg,#dc2626,#7f1d1d)" }],
  [/романт|romant|love|любов|нежн|tender/, { icon: Heart, bg: "linear-gradient(135deg,#f472b6,#be185d)" }],
  [/спокой|calm|chill|relax|тих|soft|mellow/, { icon: Leaf, bg: "linear-gradient(135deg,#34d399,#0d9488)" }],
  [/светл|happy|счаст|uplift|радост|sunny|bright/, { icon: Sun, bg: "linear-gradient(135deg,#fbbf24,#f97316)" }],
  [/мечта|dream|ethereal|космос|space|атмосфер|atmosph/, { icon: Sparkles, bg: "linear-gradient(135deg,#a78bfa,#6d28d9)" }],
  [/тревож|anxie|tense|нерв|напряж/, { icon: Activity, bg: "linear-gradient(135deg,#fb7185,#9f1239)" }],
  [/эпич|epic|грандиоз|triumph/, { icon: Crown, bg: "linear-gradient(135deg,#facc15,#a16207)" }],
];

export function moodLook(mood: string): MoodLook {
  const low = mood.toLowerCase();
  for (const [re, look] of RULES) {
    if (re.test(low)) return look;
  }
  return { icon: Music2, bg: "linear-gradient(135deg,#60a5fa,#2563eb)" };
}
