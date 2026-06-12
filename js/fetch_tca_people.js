// fetch_tca_people.js
// No dependencies needed — uses Node.js built-in fetch (Node 18+)
// Run with: node fetch_tca_people.js

import fs from "fs";

const SUPABASE_URL = "https://tlbzeciscxcyiaelsurd.supabase.co";
const SUPABASE_KEY =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRsYnplY2lzY3hjeWlhZWxzdXJkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc5Mjc2NTUsImV4cCI6MjA3MzUwMzY1NX0.d7CrsLkLVbRGdOSfbm8Zhr9RqnUZXP5bjJ133EdeHHE";

const HEADERS = {
  apikey: SUPABASE_KEY,
  Authorization: `Bearer ${SUPABASE_KEY}`,
  "Content-Type": "application/json",
};

// Full names split into [first, last] for querying
const people = [
  ["AAHIL","JASANI"],["ABDALLAH","SHABANI"],["ABDILAH","MAGARI"],["AGATA","ACKLEY"],
  ["AHMED","MOHAMED"],["ALHAJI","SADIKI"],["ALLY","CHOMBO"],["ALLY","RAJABU"],
  ["ALPHONCE","TANGAZI"],["ANACLETH","NYULA"],["ANGEL","MAJUTO"],["ANNA","PAUL"],
  ["ANUALI","ABU"],["BAHATI","KIBONA"],["BAKARI","AITANI"],["BENJAMIN","MADEDE"],
  ["BERNARD","LUKAS"],["BLESSING","MARIA"],["CHRISTOPHER","SILVANUS"],["CREDO","JOHN"],
  ["DANIEL","MABECHE"],["DAVID","LISTA"],["DEUJI","ENOCK"],["DOREEN","SHAYO"],
  ["ELIZABETH","KIULA"],["EMANUEL","ELIA"],["EZEKIEL","PATRIC"],["FATUMA","CHILUMBA"],
  ["FATUMA","HATIBU"],["FREDY","LUCAS"],["GEOFREY","ODERI"],["GETRUDA","KIMARO"],
  ["GIDEON","ABIDON"],["HALIMA","MZOBORA"],["HAMIDU","SIRA"],["HAMISI","ABDALLAH"],
  ["HAMISI","YUSUPH"],["HAPPINESS","KIMARO"],["HARSHEED","CHOHAN"],["HARUNA","BAKARI"],
  ["HASSANI","SAIDI"],["HAWA","ABAS"],["HUSNA","ALLY"],["HUSSEIN","MKISAYU"],
  ["HUSSEIN","HAIDARI"],["IDDY","LUENA"],["JACQUELINE","LYIMO"],["JASCO","MUSA"],
  ["JENNIFER","KARUME"],["JOAN","JACKSON"],["JOHN","HELANGAPI"],["JUMA","YUSUPH"],
  ["KUDRA","RAMADHANI"],["MARCO","MAKESE"],["MASOUD","RASHID"],["MATIKU","MWITA"],
  ["MECKLIN","KAVUCHA"],["MGORE","HERI"],["MICHAEL","JOSEPH"],["MOHAMED","MTIRI"],
  ["MSAFIRI","HAJI"],["MTESIGWA","BULENGA"],["MWANDE","MOHAMED"],["NEEMA","KHALFAN"],
  ["NICOLAS","MPANDULA"],["NIKOSIA","EXAVERY"],["NOELI","ELIBARIKI"],["OMARY","YASINI"],
  ["OMARY","YASIN"],["PATRICIA","NGONYAM"],["PILATO","LUBASHO"],["PRISCILLA","MMARY"],
  ["RADHIA","YUSUPH"],["RAHMA","JAMLIDI"],["REMIGIUS","REVELIAN"],["RODGERS","ANDREW"],
  ["ROSETHA","ZAWADI"],["SALOME","LEMA"],["SALUMU","PAPENI"],["SHARON","REJUS"],
  ["SILVESTER","ALBERT"],["STEPHANO","BELLEGE"],["STEPHEN","MNGARA"],["STEVEN","MAIKO"],
  ["SWALEHE","HAMZA"],["SWALEHE","MAHIMBO"],["TATU","MASANJA"],["UPENDO","ELIAS"],
  ["WINNIE","CHISONJEZA"],["ZUBERI","MOHAMEDI"],
];

async function fetchAll() {
  // Fetch ALL records from the table (paginate if needed)
  let allRecords = [];
  let from = 0;
  const pageSize = 1000;

  while (true) {
    const url = `${SUPABASE_URL}/rest/v1/tca_db_people_record?select=*&order=id.asc&offset=${from}&limit=${pageSize}`;
    const res = await fetch(url, { headers: HEADERS });
    const data = await res.json();

    if (!Array.isArray(data)) {
      console.error("Unexpected response:", JSON.stringify(data));
      process.exit(1);
    }

    allRecords = allRecords.concat(data);
    if (data.length < pageSize) break;
    from += pageSize;
  }

  return allRecords;
}

async function main() {
  console.log(`Fetching all records from tca_db_people_record...`);
  const allRecords = await fetchAll();
  console.log(`Total records in table: ${allRecords.length}`);

  // Build a lookup map: "FIRSTNAME|LASTNAME" -> record
  const lookup = new Map();
  for (const r of allRecords) {
    const key = `${(r.first_name || "").toUpperCase()}|${(r.last_name || "").toUpperCase()}`;
    lookup.set(key, r);
  }

  // Match each requested person
  const matched = [];
  const notFound = [];

  for (const [first, last] of people) {
    const key = `${first}|${last}`;
    if (lookup.has(key)) {
      matched.push(lookup.get(key));
    } else {
      notFound.push(`${first} ${last}`);
    }
  }

  console.log(`\n✅ Matched: ${matched.length} / ${people.length}`);

  if (notFound.length > 0) {
    console.log(`\n⚠️  Not found (${notFound.length}):`);
    notFound.forEach((n) => console.log("  -", n));
  }

  // Save matched records to JSON
  fs.writeFileSync("tca_people_results.json", JSON.stringify(matched, null, 2));
  console.log("\n💾 Matched records saved to tca_people_results.json");

  // Also save a CSV
  if (matched.length > 0) {
    const cols = Object.keys(matched[0]);
    const csvLines = [
      cols.join(","),
      ...matched.map((r) =>
        cols.map((c) => {
          const val = r[c] == null ? "" : String(r[c]);
          return val.includes(",") || val.includes('"') || val.includes("\n")
            ? `"${val.replace(/"/g, '""')}"`
            : val;
        }).join(",")
      ),
    ];
    fs.writeFileSync("tca_people_results.csv", csvLines.join("\n"));
    console.log("💾 CSV saved to tca_people_results.csv");
  }

  // Preview
  console.log("\nPreview (first 3 matched):");
  console.log(JSON.stringify(matched.slice(0, 3), null, 2));
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});