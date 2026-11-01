import React, { useEffect, useRef, useState } from "react";
type Pref = {
  version: number;
  scene: string;
  motion: boolean;
  opaque: boolean;
  fit: string;
  image: Blob | null;
};
const defaults: Pref = {
  version: 2,
  scene: "video",
  motion: true,
  opaque: false,
  fit: "cover",
  image: null,
};
function db(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open("orb-appearance", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("preferences");
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
    r.onblocked = () => reject(Error("Browser appearance storage is busy"));
  });
}
async function storage(value?: Pref) {
  const d = await db();
  try {
    return await new Promise<Pref | undefined>((resolve, reject) => {
      const t = d.transaction("preferences", value ? "readwrite" : "readonly"),
        s = t.objectStore("preferences");
      if (value) {
        s.put(value, "appearance");
        t.oncomplete = () => resolve(value);
      } else {
        const r = s.get("appearance");
        r.onsuccess = () => resolve(r.result);
      }
      t.onerror = () => reject(t.error);
    });
  } finally {
    d.close();
  }
}
export function Logo({ large = false }: { large?: boolean }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const update = () => {
      const v = ref.current;
      if (!v) return;
      if (
        document.hidden ||
        document.body.dataset.motion === "false" ||
        matchMedia("(prefers-reduced-motion: reduce)").matches
      )
        v.pause();
      else v.play().catch(() => {});
    };
    const obs = new MutationObserver(update);
    obs.observe(document.body, {
      attributes: true,
      attributeFilter: ["data-motion"],
    });
    document.addEventListener("visibilitychange", update);
    const q = matchMedia("(prefers-reduced-motion: reduce)");
    q.addEventListener("change", update);
    update();
    return () => {
      obs.disconnect();
      document.removeEventListener("visibilitychange", update);
      q.removeEventListener("change", update);
    };
  }, []);
  return (
    <video
      ref={ref}
      className={large ? "orb-video large-orb" : "orb-video"}
      autoPlay
      muted
      loop
      playsInline
      poster="/assets/droplet-poster.jpg"
      src="/assets/droplet.mp4"
      aria-label="Liquid glass ORB logo"
    />
  );
}
export default function Appearance({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [pref, setPref] = useState<Pref>(defaults),
    [ready, setReady] = useState(false),
    [url, setURL] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const video = useRef<HTMLVideoElement>(null),
    dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    let live = true;
    storage()
      .then((p) => {
        if (live && p) setPref({ ...defaults, ...p });
      })
      .catch(() => setError("Appearance storage is unavailable."))
      .finally(() => setReady(true));
    return () => {
      live = false;
    };
  }, []);
  useEffect(() => {
    if (pref.image) {
      const src = URL.createObjectURL(pref.image);
      setURL(src);
      return () => URL.revokeObjectURL(src);
    }
    setURL("");
  }, [pref.image]);
  const isVideo =
    pref.scene === "video" ||
    (pref.scene === "custom" && pref.image?.type.startsWith("video/"));
  useEffect(() => {
    document.body.dataset.motion = String(pref.motion);
    document.body.dataset.opaque = String(pref.opaque);
    document.body.dataset.scene = pref.scene;
    document.body.dataset.videoWallpaper = String(!!isVideo);
    document.body.dataset.fit = pref.fit;
  }, [pref, isVideo]);
  useEffect(() => {
    const update = () => {
      const v = video.current;
      if (!v) return;
      if (
        !pref.motion ||
        document.hidden ||
        matchMedia("(prefers-reduced-motion: reduce)").matches
      )
        v.pause();
      else v.play().catch(() => {});
    };
    update();
    document.addEventListener("visibilitychange", update);
    const q = matchMedia("(prefers-reduced-motion: reduce)");
    q.addEventListener("change", update);
    return () => {
      document.removeEventListener("visibilitychange", update);
      q.removeEventListener("change", update);
    };
  }, [pref.motion, isVideo, url]);
  useEffect(() => {
    const move = (e: PointerEvent) => {
      if (
        !pref.motion ||
        e.pointerType === "touch" ||
        matchMedia("(prefers-reduced-motion: reduce)").matches
      )
        return;
      document.documentElement.style.setProperty(
        "--mx",
        `${(e.clientX / innerWidth - 0.5) * -22}px`,
      );
      document.documentElement.style.setProperty(
        "--my",
        `${(e.clientY / innerHeight - 0.5) * -16}px`,
      );
    };
    window.addEventListener("pointermove", move, { passive: true });
    return () => window.removeEventListener("pointermove", move);
  }, [pref.motion]);
  useEffect(() => {
    if (open) dialog.current?.showModal();
    else dialog.current?.close();
  }, [open]);
  async function save(patch: Partial<Pref>) {
    setBusy(true);
    setError("");
    try {
      const next = { ...pref, ...patch };
      await storage(next);
      setPref(next);
    } catch {
      setError(
        "Could not save wallpaper. Check browser storage or choose a smaller file.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      {isVideo ? (
        <video
          className="video-wallpaper"
          ref={video}
          key={pref.scene === "custom" ? url : "default"}
          autoPlay
          muted
          loop
          playsInline
          preload="auto"
          src={pref.scene === "custom" ? url : "/assets/wallpaper-default.mp4"}
          poster="/assets/wallpaper-poster.png"
          style={{ objectFit: pref.fit as "cover" | "contain" }}
          aria-hidden="true"
        />
      ) : (
        <div
          className="landscape"
          style={
            pref.scene === "custom"
              ? { backgroundImage: `url("${url}")` }
              : undefined
          }
          aria-hidden="true"
        />
      )}
      <div className="atmosphere" aria-hidden="true" />
      <dialog ref={dialog} className="glass drawer" onCancel={onClose}>
        <div className="drawer-head">
          <h2>Appearance</h2>
          <button
            className="icon"
            onClick={onClose}
            aria-label="Close appearance"
          >
            ×
          </button>
        </div>
        {error && (
          <p role="alert" className="warning">
            {error}
          </p>
        )}
        <fieldset disabled={busy || !ready}>
          <button
            className={`scene video-scene ${pref.scene === "video" ? "selected" : ""}`}
            onClick={() => save({ scene: "video" })}
          >
            Liquid glass <small>Default · video</small>
          </button>
          <button
            className={`scene ${pref.scene === "alpine" ? "selected" : ""}`}
            onClick={() => save({ scene: "alpine" })}
          >
            Obsidian
          </button>
          <button
            className={`scene alt ${pref.scene === "mist" ? "selected" : ""}`}
            onClick={() => save({ scene: "mist" })}
          >
            Midnight blue
          </button>
          {pref.image && (
            <button
              className="saved-background"
              onClick={() => save({ scene: "custom" })}
            >
              Your saved{" "}
              {pref.image.type.startsWith("video/") ? "video" : "image"} ↗
            </button>
          )}
          <label className="upload-label">
            Choose image or video
            <input
              type="file"
              accept="image/jpeg,image/png,image/webp,video/mp4,video/webm"
              onChange={async (e) => {
                const f = e.target.files?.[0];
                if (!f) return;
                if (
                  ![
                    "image/jpeg",
                    "image/png",
                    "image/webp",
                    "video/mp4",
                    "video/webm",
                  ].includes(f.type) ||
                  f.size > 150 * 1024 * 1024
                ) {
                  setError("Use JPG, PNG, WebP, MP4 or WebM, up to 150 MB.");
                  return;
                }
                if (
                  f.type.startsWith("video/") &&
                  !document.createElement("video").canPlayType(f.type)
                ) {
                  setError("This browser cannot play that video type.");
                  return;
                }
                await save({ scene: "custom", image: f });
              }}
            />
          </label>
          <p className="appearance-note">
            Original quality · saved in this browser.
          </p>
          <label className="field">
            Display
            <select
              value={pref.fit}
              onChange={(e) => save({ fit: e.target.value })}
            >
              <option value="cover">Fill screen</option>
              <option value="contain">Fit entire image / video</option>
            </select>
          </label>
          <label className="appearance-row">
            Background motion
            <input
              type="checkbox"
              checked={pref.motion}
              onChange={(e) => save({ motion: e.target.checked })}
            />
          </label>
          <label className="appearance-row">
            Reduce transparency
            <input
              type="checkbox"
              checked={pref.opaque}
              onChange={(e) => save({ opaque: e.target.checked })}
            />
          </label>
        </fieldset>
      </dialog>
    </>
  );
}
