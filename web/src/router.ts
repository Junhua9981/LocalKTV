import { createRouter, createWebHistory } from "vue-router";
import DisplayView from "./views/DisplayView.vue";
import HostView from "./views/HostView.vue";
import RemoteView from "./views/RemoteView.vue";
import RoomsView from "./views/RoomsView.vue";
import SongsView from "./views/SongsView.vue";
import { BASE } from "./base";

export const router = createRouter({
  history: createWebHistory(BASE + "/"),
  routes: [
    // the room list; every page lives in a room (包廂): /r/<room>/display
    { path: "/", component: RoomsView },
    // admin: the shared song cache (fix lyrics / audio source / MV)
    { path: "/songs", component: SongsView },
    { path: "/r/:room/display", component: DisplayView, meta: { role: "display" } },
    { path: "/r/:room/host", component: HostView, meta: { role: "host" } },
    { path: "/r/:room/remote", component: RemoteView, meta: { role: "remote" } },
    // pre-rooms URLs (bookmarks, paired TVs) open the main room
    { path: "/display", redirect: "/r/main/display" },
    { path: "/host", redirect: "/r/main/host" },
    { path: "/remote", redirect: "/r/main/remote" },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});
