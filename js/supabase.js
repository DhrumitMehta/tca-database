// js/supabase.js — initialise Supabase client
// Import this before auth.js and any other scripts that use `supabase`
 
import { createClient } from 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js/+esm';

const SUPABASE_URL = "https://tlbzeciscxcyiaelsurd.supabase.co";
const SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRsYnplY2lzY3hjeWlhZWxzdXJkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc5Mjc2NTUsImV4cCI6MjA3MzUwMzY1NX0.d7CrsLkLVbRGdOSfbm8Zhr9RqnUZXP5bjJ133EdeHHE";

window.supabase = createClient(SUPABASE_URL, SUPABASE_ANON);