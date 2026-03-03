/**
 * fetch_item_details.mjs <item_id>
 * Fetches item details + EU auction house prices from TLDB's internal API.
 * Outputs clean JSON to stdout.
 */

import * as devalue from "devalue";
import { decompress } from "compress-json";

const item_id = process.argv[2];
if (!item_id) {
  console.error("Usage: node fetch_item_details.mjs <item_id>");
  process.exit(1);
}

const EU_SERVERS = {
  "30001": "EU-1",
  "30003": "EU-2",
  "30004": "EU-3",
};

// ── Fetch item details ────────────────────────────────────────────────────────

const itemResp = await fetch(
  `https://tldb.info/db/item/${item_id}/__data.json`,
  { headers: { "User-Agent": "Mozilla/5.0" } }
).catch((e) => { console.error("NETWORK_ERROR:" + e.message); process.exit(1); });

if (!itemResp.ok) {
  console.error("HTTP_ERROR:" + itemResp.status);
  process.exit(1);
}

const json = await itemResp.json();
const dataNode = json.nodes?.find((e) => e?.type === "data");
if (!dataNode) { console.error("NO_DATA_NODE"); process.exit(1); }

const apiData = devalue.unflatten(dataNode.data);
const item = apiData?.data;
if (!item) { console.error("NO_ITEM"); process.exit(1); }

// ── Fetch AH prices ───────────────────────────────────────────────────────────

let eu_prices = {};

try {
  const priceResp = await fetch("https://tldb.info/api/ah/prices", {
    headers: { "User-Agent": "Mozilla/5.0" },
  });

  if (priceResp.ok) {
    const priceData = await priceResp.json();
    const itemNum = item.num;

    for (const [serverId, serverName] of Object.entries(EU_SERVERS)) {
      const raw = priceData.list?.[serverId];
      if (!raw) continue;

      const serverPrices = decompress(JSON.parse(raw));
      const entry = serverPrices[itemNum];

      if (entry && entry.quantity > 0) {
        eu_prices[serverName] = {
          price: entry.price,
          quantity: entry.quantity,
        };
      } else {
        eu_prices[serverName] = null; // not listed
      }
    }
  }
} catch (e) {
  // AH prices are optional — don't fail if unavailable
  console.error("Warning: could not fetch AH prices:", e.message);
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const ENCHANT_LEVEL = 12;

function formatValue(value, format, multiply) {
  const computed = value * (multiply ?? 1);
  const rounded = Math.round(computed * 100) / 100;
  if (!format || format === "{0}") return String(rounded);
  return format.replace("{0}", rounded);
}

function computeStatAtLevel(stat, level) {
  if (level === 0) return stat.value;
  const bonus = stat.enchant_values?.[level - 1] ?? 0;
  return stat.value + bonus;
}

// ── Parse main stats at enchant level 12 ─────────────────────────────────────
// Damage stats need special handling: min = attack_power, max = attack_power + bonus_attack_power

function buildMainStats(statList) {
  if (!Array.isArray(statList)) return [];

  const statMap = {};
  for (const s of statList) {
    statMap[s.key] = computeStatAtLevel(s, ENCHANT_LEVEL);
  }

  const results = [];
  const handled = new Set();

  // Pair attack_power + bonus into a min~max range
  const damagePairs = [
    ["attack_power_main_hand",  "bonus_attack_power_main_hand",  "Damage"],
    ["attack_power_off_hand",   "bonus_attack_power_off_hand",   "Off-Hand Damage"],
  ];

  for (const [mainKey, bonusKey, label] of damagePairs) {
    if (statMap[mainKey] !== undefined) {
      const attackPower = statMap[mainKey];
      const bonus = statMap[bonusKey] ?? 0;

      const min = bonus + 1;
      const max = attackPower + bonus;

      const stat = statList.find(s => s.key === mainKey);
      const minFmt = formatValue(min, stat.format, stat.multiply);
      const maxFmt = formatValue(max, stat.format, stat.multiply);

      results.push({
        key: mainKey,
        name: label,
        value: `${minFmt} ~ ${maxFmt}`,
      });
      handled.add(mainKey);
      handled.add(bonusKey);
    }
  }

  // All other stats (Attack Speed, Range, etc.)
  for (const stat of statList) {
    if (handled.has(stat.key)) continue;
    const raw = computeStatAtLevel(stat, ENCHANT_LEVEL);
    results.push({
      key: stat.key,
      name: stat.name,
      value: formatValue(raw, stat.format, stat.multiply),
    });
  }

  return results;
}

function parseStatGroup(statList) {
  if (!Array.isArray(statList)) return [];
  return statList.map((stat) => ({
    key: stat.key,
    name: stat.name,
    value: formatValue(computeStatAtLevel(stat, ENCHANT_LEVEL), stat.format, stat.multiply),
  }));
}

// ── Build output ──────────────────────────────────────────────────────────────

const stats = item.stats ?? {};
const skill = item.skill_unique ?? null;

process.stdout.write(JSON.stringify({
  id: item.id,
  num: item.num,
  name: item.name,
  rarity: item.rarity,
  icon: item.icon,
  description: item.description,
  type: item.typeName,
  skill: skill ? {
    name: skill.name,
    description: Array.isArray(skill.description)
      ? skill.description.join("")
      : skill.description ?? "",
  } : null,
  main_stats: buildMainStats(stats.main_stats),
  extra_stats: parseStatGroup(stats.extra_fixed_stats),
  enchant_level: 12,
  eu_prices,
}));
