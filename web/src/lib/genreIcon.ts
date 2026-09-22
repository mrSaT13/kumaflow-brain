import {
  AudioLines,
  Church,
  Disc3,
  Drama,
  Drum,
  Ghost,
  Globe,
  Guitar,
  Heart,
  Leaf,
  Mic,
  Moon,
  Music2,
  Piano,
  Radio,
  Skull,
  Smile,
  Snowflake,
  Star,
  Sun,
  Flame,
  Zap,
  type LucideIcon,
} from "lucide-react";

// Правила сверху вниз: специфичные раньше общих.
// Составные жанры ("Hip Hop;Pop", "Alternative Metal / Post-Grunge") режем на части,
// побеждает первое совпавшее правило.
const RULES: [RegExp, LucideIcon][] = [
  [/death|black metal|doom|grind|blackened/, Skull],
  [/goth|darkwave/, Ghost],
  [/christmas|holiday|xmas/, Snowflake],
  [/kids|children|disney|lullaby/, Smile],
  [/soundtrack|musical|cinematic|score/, Drama],
  [/classical|baroque|piano|neoclassical|orchest|symphon|chamber|opera/, Piano],
  [/country|folk|americana|bluegrass|singer-songwriter/, Leaf],
  [/reggae|ska|dancehall|afro|salsa|samba|flamenco|celtic|latin|bossa/, Sun],
  [/asian|indian|bollywood|arab|turkish|japanese|chinese|enka|city pop/, Globe],
  [/gospel|christian|worship/, Church],
  [/spoken|podcast|audiobook|comedy|radio|news|^talk/, Radio],
  [/workout|sport|gym|running|fitness/, Zap],
  [/sleep|night/, Moon],
  [/love|romantic|ballad|wedding/, Heart],
  [/metal|djent|metalcore|deathcore|nu metal/, Flame],
  [/rock|punk|hardcore|emo|grunge|indie|alternative|alt\.|post-|prog|garage|britpop|stoner|psych|acoustic|noise/, Guitar],
  [/house|trance|dubstep|techno|edm|disco|\bdance|club|eurodance|big room|hardstyle|drum.?n.?bass|jungle/, Disc3],
  [/hip hop|hip-hop|\brap\b|trap|gangsta|drill|phonk|boom bap|grime/, Mic],
  [/electronic|ambient|downtempo|chill|lounge|trip|experimental|abstract|idm|lofi|lo-fi|new age|vapor|synthwave|minimal/, AudioLines],
  [/jazz|blues|soul|funk|swing|r&b|\brnb\b/, Drum],
  [/k-pop|j-pop|synth-pop|electropop|synthpop|c-pop/, Star],
  [/pop|boyband|girl group|idol/, Star],
];

export function genreIcon(name: string): LucideIcon {
  const parts = name.toLowerCase().split(/[,/;|&+]+/).map((s) => s.trim()).filter(Boolean);
  for (const [re, Icon] of RULES) {
    if (parts.some((p) => re.test(p))) return Icon;
  }
  return Music2;
}
