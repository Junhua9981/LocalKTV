<script setup lang="ts">
/**
 * 首頁：選這台要當螢幕還是遙控器。管理員（或沒開登入的區網）才會看到這頁；
 * 客人和電視會被伺服器直接送進自己那間。只有一間房時不提「房間」。
 */
import { computed, onMounted, onUnmounted, ref } from "vue";
import { roomUrl, url } from "../base";

interface RoomRow {
  slug: string;
  name: string;
  displays?: number;
  phones?: number;
  queue?: number;
  current?: string;
}

const rooms = ref<RoomRow[]>([]);
const authOn = ref(false);
const error = ref("");
const slug = ref("");
const name = ref("");
const busy = ref(false);
let timer: number | undefined;
const single = computed(() => rooms.value.length === 1);
const site = location.origin + url("/");

async function load() {
  try {
    const res = await fetch(url("/api/rooms"), { cache: "no-store" });
    if (res.status === 401) {
      location.reload(); // the login gate takes over
      return;
    }
    rooms.value = await res.json();
    const rows = rooms.value;
    // a guest / TV only ever sees its own room: go straight there
    if (rows.length === 1 && rows[0].displays === undefined) {
      location.replace(url(roomUrl(rows[0].slug, "/remote")));
    }
  } catch {
    error.value = "連不到伺服器";
  }
}

async function create() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const res = await fetch(url("/api/rooms"), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ slug: slug.value.trim().toLowerCase(), name: name.value.trim() }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      error.value = body.detail ?? `新增失敗（${res.status}）`;
      return;
    }
    slug.value = "";
    name.value = "";
    await load();
  } finally {
    busy.value = false;
  }
}

async function remove(row: RoomRow) {
  if (!confirm(`刪除「${row.name}」？這間的清單會清空，電視和客人會被登出。`)) return;
  const res = await fetch(url(`/api/rooms/${encodeURIComponent(row.slug)}`), { method: "DELETE" });
  if (!res.ok) error.value = `刪除失敗（${res.status}）`;
  await load();
}

onMounted(async () => {
  try {
    const health = await (await fetch(url("/api/health"), { cache: "no-store" })).json();
    authOn.value = !!health.auth;
  } catch { /* load() reports it */ }
  await load();
  timer = window.setInterval(load, 5000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <main class="rooms">
    <h1 class="logo">LOCAL<b>KTV</b></h1>
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <section v-for="row in rooms" :key="row.slug" class="room">
      <div v-if="!single" class="head">
        <h2>{{ row.name }}</h2>
        <code>/r/{{ row.slug }}</code>
      </div>
      <p v-if="row.displays !== undefined" class="stat">
        螢幕 {{ row.displays ?? 0 }}・手機 {{ row.phones ?? 0 }}・清單 {{ row.queue ?? 0 }} 首
        <span v-if="row.current">・正在唱《{{ row.current }}》</span>
      </p>
      <div class="go">
        <a class="big" :href="url(roomUrl(row.slug, '/display'))">
          <span class="icon">📺</span><b>這台當螢幕</b><small>筆電或電視，接喇叭顯示歌詞</small>
        </a>
        <a class="big" :href="url(roomUrl(row.slug, '/remote'))">
          <span class="icon">🎤</span><b>這台當遙控器</b><small>搜尋、點歌、切歌</small>
        </a>
      </div>
      <div class="links">
        <a :href="url(roomUrl(row.slug, '/host'))">主控台</a>
        <button v-if="row.slug !== 'main' && !authOn" class="ghost" @click="remove(row)">刪除</button>
      </div>
    </section>

    <section class="tips">
      <p v-if="authOn">
        <b>別台裝置當螢幕：</b>在那台打開 <code>{{ site }}</code>，用這支手機掃它畫面上的 QR。
      </p>
      <p><b>朋友點歌：</b>掃螢幕上的點歌 QR，不用登入。</p>
    </section>

    <details class="more">
      <summary>新增房間（同時給不同的人各自唱）</summary>
      <p class="hint">每間房有自己的螢幕、遙控器和點歌清單，歌曲快取共用。</p>
      <form class="create" @submit.prevent="create">
        <input v-model="slug" maxlength="16" pattern="[a-z0-9][a-z0-9\-]{0,15}" placeholder="房號（例：a、vip）" required />
        <input v-model="name" maxlength="24" placeholder="名稱（例：小明家）" />
        <button :disabled="busy">新增房間</button>
      </form>
    </details>
    <p v-if="authOn" class="hint">
      <a :href="url('/admin')">管理頁</a>：已登入的裝置、結束這場、房間改名或刪除、改密碼。
    </p>
    <p class="hint">
      <a :href="url('/songs')">歌曲快取</a>：歌詞錯了、版本不對、MV 不對，在這裡修。
    </p>
  </main>
</template>

<style scoped>
.rooms {
  max-width: 640px;
  margin: 0 auto;
  padding: 24px 16px 48px;
}
.logo {
  font: 700 28px/1 "Chakra Petch", system-ui, sans-serif;
  letter-spacing: 0.06em;
}
.logo b {
  color: #19f0ff;
}
.hint,
.stat {
  color: var(--fg-dim);
  font-size: 14px;
}
.hint a,
.tips a {
  color: #19f0ff;
}
.error {
  color: #ff6b8a;
}
.room {
  margin: 14px 0;
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--bg-2);
}
.head {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.head h2 {
  margin: 0;
  font-size: 20px;
}
.head code {
  color: var(--fg-dim);
  font-size: 13px;
}
.links {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.links a {
  padding: 8px 14px;
  border: 1px solid var(--line);
  border-radius: 12px;
  color: var(--fg);
  text-decoration: none;
  background: var(--bg-3);
}
.links .ghost {
  margin-left: auto;
}
.go {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px;
  margin: 4px 0 12px;
}
.big {
  display: grid;
  gap: 4px;
  padding: 18px 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--bg-3);
  color: var(--fg);
  text-decoration: none;
}
.big:hover,
.big:focus-visible {
  border-color: #19f0ff;
}
.big .icon {
  font-size: 28px;
}
.big b {
  font-size: 20px;
}
.big small {
  color: var(--fg-dim);
}
.tips {
  color: var(--fg-dim);
  font-size: 14px;
}
.tips code {
  color: var(--fg);
  word-break: break-all;
}
.more {
  margin: 20px 0 8px;
}
.more summary {
  cursor: pointer;
  color: var(--fg-dim);
}
.create {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 20px;
}
.create input {
  flex: 1 1 140px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--bg-3);
  color: var(--fg);
  font: inherit;
}
</style>
