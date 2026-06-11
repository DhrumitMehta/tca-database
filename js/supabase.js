// js/supabase.js
const { createClient } = supabase;  // from the CDN bundle above

const SUPABASE_URL = "https://tlbzeciscxcyiaelsurd.supabase.co";
const SUPABASE_ANON = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRsYnplY2lzY3hjeWlhZWxzdXJkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc5Mjc2NTUsImV4cCI6MjA3MzUwMzY1NX0.d7CrsLkLVbRGdOSfbm8Zhr9RqnUZXP5bjJ133EdeHHE";

window.supabase = createClient(SUPABASE_URL, SUPABASE_ANON);