// Каноничный цвет жанра — по СЕМЕЙСТВУ, а не по хэшу полной строки.
//
// Проблема: кружки красились хэшем от всего названия, поэтому
// «Alternative Metal» и «Alternative Metal / Post-Grunge» получали
// разные цвета. Правила — в том же порядке, что в genreIcon:
// составной жанр режем на части, побеждает первое совпавшее правило,
// так что цвет всегда совпадает с семейством иконки.
const RULES: [RegExp, string][] = [
  [/death|black metal|doom|grind|blackened/, "#7C3AED"],
  [/goth|darkwave/, "#64748B"],
  [/christmas|holiday|xmas/, "#5AC8FA"],
  [/kids|children|disney|lullaby/, "#FFCC00"],
  [/soundtrack|musical|cinematic|score/, "#8E8E93"],
  [/classical|baroque|piano|neoclassical|orchest|symphon|chamber|opera/, "#C9A227"],
  [/country|folk|americana|bluegrass|singer-songwriter/, "#34C759"],
  [/reggae|ska|dancehall|afro|salsa|samba|flamenco|celtic|latin|bossa/, "#FF9500"],
  [/asian|indian|bollywood|arab|turkish|japanese|chinese|enka|city pop/, "#007AFF"],
  [/gospel|christian|worship/, "#AF52DE"],
  [/spoken|podcast|audiobook|comedy|radio|news|^talk/, "#8E8E93"],
  [/workout|sport|gym|running|fitness/, "#FF3B30"],
  [/sleep|night/, "#5856D6"],
  [/love|romantic|ballad|wedding/, "#FF2D55"],
  [/metal|djent|metalcore|deathcore|nu metal/, "#F97316"],
  [/rock|punk|hardcore|emo|grunge|indie|alternative|alt\.|post-|prog|garage|britpop|stoner|psych|acoustic|noise/, "#EF4444"],
  [/house|trance|dubstep|techno|edm|disco|\bdance|club|eurodance|big room|hardstyle|drum.?n.?bass|jungle/, "#22D3EE"],
  [/hip hop|hip-hop|\brap\b|trap|gangsta|drill|phonk|boom bap|grime/, "#A855F7"],
  [/electronic|ambient|downtempo|chill|lounge|trip|experimental|abstract|idm|lofi|lo-fi|new age|vapor|synthwave|minimal/, "#34D399"],
  [/jazz|blues|soul|funk|swing|r&b|\brnb\b/, "#0EA5E9"],
  [/k-pop|j-pop|synth-pop|electropop|synthpop|c-pop/, "#EC4899"],
  [/pop|boyband|girl group|idol/, "#F472B6"],
];

const FALLBACK = ["#FF3B30", "#007AFF", "#34C759", "#5856D6", "#AF52DE", "#FF9500", "#FF2D55", "#5AC8FA", "#00C7BE", "#FF9F0A"];

export function hashColor(s: string): string {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return FALLBACK[h % FALLBACK.length];
}

export function genreColor(name: string): string {
  const parts = name.toLowerCase().split(/[,/;|&+]+/).map((s) => s.trim()).filter(Boolean);
  for (const [re, color] of RULES) {
    if (parts.some((p) => re.test(p))) return color;
  }
  return hashColor(name);
}
