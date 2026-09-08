/* Persistent metadata only: never put video bytes or credentials in IndexedDB. */
window.ArchangelMedia = {
  async mount(root, api, account) {
    try { await api("GET", "/hs/media/capabilities"); } catch (_) { return; }
    if (!root.isConnected) return;
    const box = document.createElement("section");
    box.className = "asc-card-pad";
    box.innerHTML = '<h3>Large files and video</h3><p>Upload files or a folder. Keep this tab open while sending. After reopening, choose the same files to resume. Originals stay on hold until reviewed.</p><label>Files <input type="file" multiple data-files></label> <label>Folder <input type="file" multiple webkitdirectory data-folder></label> <button type="button" data-pause>Pause</button> <button type="button" data-new>New collection</button><ul aria-live="polite"></ul>';
    root.appendChild(box);
    const list = box.querySelector("ul");
    const scope = location.pathname.startsWith("/sandbox") ? "sandbox" : "live";
    const db = await new Promise((resolve, reject) => {
      const req = indexedDB.open("archangel-media", 1);
      req.onupgradeneeded = () => req.result.createObjectStore("queue");
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    const key = scope + ":" + account;
    async function persisted(value, recordKey = key) {
      return new Promise((resolve, reject) => {
        const tx = db.transaction("queue", value === undefined ? "readonly" : "readwrite");
        const store = tx.objectStore("queue");
        const req = value === undefined ? store.get(recordKey) : store.put(value, recordKey);
        tx.oncomplete = () => resolve(req.result);
        tx.onerror = () => reject(tx.error);
      });
    }
    let saved = await persisted() || {collection: null}, paused = false, running = false;
    const pause = box.querySelector("[data-pause]");
    pause.onclick = () => { paused = !paused; pause.textContent = paused ? "Resume" : "Pause"; };
    box.querySelector("[data-new]").onclick = async () => {
      if (running) return;
      saved = {collection: null};
      await persisted(saved);
      list.replaceChildren();
    };
    const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
    async function checksum(blob) {
      const worker = new Worker("/static/provider/media-hash-worker.js");
      try {
        return await new Promise((resolve, reject) => {
          worker.onmessage = ({data}) => data.error ? reject(new Error(data.error)) : resolve(data.checksum);
          worker.onerror = () => reject(new Error("File checksum failed."));
          worker.postMessage(blob);
        });
      } finally { worker.terminate(); }
    }
    async function upload(files) {
      if (running) return;
      running = true;
      for (const input of box.querySelectorAll("input")) input.disabled = true;
      try {
        if (!saved.collection) {
          saved.collection = (await api("POST", "/hs/media/collections", {})).id;
          await persisted(saved);
        }
        for (const file of files) {
          if (!box.isConnected) break;
          const item = document.createElement("li");
          const label = document.createElement("span");
          const cancel = document.createElement("button");
          cancel.type = "button"; cancel.textContent = "Cancel";
          item.append(label, cancel); list.appendChild(item);
          const path = file.webkitRelativePath || file.name;
          const identity = JSON.stringify([path, file.size, file.lastModified]);
          // Metadata identity finds the session; every stored part is then
          // rehashed against S3, detecting a changed file with the same metadata.
          const entryKey = key + ":" + saved.collection + ":" + identity;
          let entry = await persisted(undefined, entryKey), cancelled = false;
          cancel.onclick = async () => {
            cancelled = true;
            if (entry?.id) {
              try { await api("DELETE", "/hs/media/files/" + entry.id); label.textContent = path + ": cancellation requested"; }
              catch (e) { label.textContent = path + ": " + e.message; }
            }
          };
          try {
            if (!entry) { entry = {token: crypto.randomUUID()}; await persisted(entry, entryKey); }
            let row = await api("POST", "/hs/media/files", {collection: saved.collection, token: entry.token, path, size: file.size});
            entry.id = row.id; await persisted(entry, entryKey);
            row = await api("GET", "/hs/media/files/" + row.id);
            if (row.state !== "uploading") { label.textContent = path + ": " + row.state; cancel.disabled = true; continue; }
            const received = new Map(row.parts.map(p => [p.number, p.checksum]));
            for (let n = 1; n <= row.part_count; n++) {
              while (paused && !cancelled && box.isConnected) await wait(250);
              if (!box.isConnected) throw new Error("Upload paused. Reopen uploads to resume.");
              if (cancelled) break;
              const blob = file.slice((n-1)*row.chunk_size, n*row.chunk_size);
              label.textContent = path + ": checking part " + n + "/" + row.part_count;
              const sum = await checksum(blob);
              if (received.has(n)) {
                if (received.get(n) !== sum) throw new Error("File changed. Start a new collection to upload it.");
                continue;
              }
              for (let attempt = 0; ; attempt++) {
                if (cancelled) break;
                try {
                  const signed = await api("POST", "/hs/media/files/" + row.id + "/sign", {number: n, checksum: sum});
                  const response = await fetch(signed.url, {method: "PUT", body: blob, headers: {"x-amz-checksum-sha256": sum}, credentials: "omit"});
                  if (!response.ok) throw new Error("Part upload failed; choose this file again to resume.");
                  break;
                } catch (e) {
                  if (attempt >= 4) throw e;
                  await wait(Math.min(16000, 1000*2**attempt) + Math.random()*300);
                }
              }
              label.textContent = path + ": " + Math.round(n/row.part_count*100) + "% sent";
            }
            if (!cancelled) {
              await api("POST", "/hs/media/files/" + row.id + "/complete", {});
              label.textContent = path + ": received; storage verification queued";
              cancel.disabled = true;
            } else if (entry.id) { await api("DELETE", "/hs/media/files/" + entry.id); }
          } catch (e) { label.textContent = path + ": " + e.message; }
        }
      } catch (e) { const li = document.createElement("li"); li.textContent = e.message; list.appendChild(li); }
      finally {
        running = false;
        for (const input of box.querySelectorAll("input")) { input.disabled = false; input.value = ""; }
      }
    }
    for (const input of box.querySelectorAll("input")) input.onchange = () => upload(Array.from(input.files));
    if (saved.collection) {
      const li = document.createElement("li");
      li.textContent = "Previous collection saved. Choose the same files to resume or check their receipts.";
      list.appendChild(li);
    }
  }
};
