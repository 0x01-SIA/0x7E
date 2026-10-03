(() => {
  const list = document.querySelector("#messages");
  const sendForm = document.querySelector("#send-form");
  const input = document.querySelector("#message-input");
  const status = document.querySelector("#status");
  const button = document.querySelector("#send-button");
  let previous = "";

  function showStatus(message, error = false) {
    status.textContent = message;
    status.classList.toggle("error", error);
  }

  async function refresh() {
    try {
      const response = await fetch("/api/messages", { cache: "no-store" });
      if (!response.ok) return;
      const data = await response.json();
      const signature = JSON.stringify(data.messages);
      if (signature === previous) return;
      previous = signature;
      list.replaceChildren();
      if (!data.messages.length) {
        const empty = document.createElement("p");
        empty.className = "empty";
        empty.textContent = "Waiting for messages…";
        list.append(empty);
        return;
      }
      for (const message of data.messages) {
        const article = document.createElement("article");
        article.className = "message" + (message.origin_node === data.node_id ? " local" : "");
        const meta = document.createElement("div");
        meta.className = "message-meta";
        const sender = document.createElement("strong");
        sender.textContent = `[${new Date(message.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}] ${message.sender}`;
        meta.append(sender, document.createTextNode(` @ Node ${message.origin_node} → @${message.destination === "ALL" ? "all" : message.destination}`));
        const body = document.createElement("div");
        body.className = "message-body";
        body.textContent = message.body;
        article.append(meta, body);
        list.append(article);
      }
      list.scrollTop = list.scrollHeight;
    } catch (_) { /* The next poll will retry while the hotspot remains available. */ }
  }

  sendForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    showStatus("Sending…");
    try {
      const response = await fetch("/api/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: input.value })
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not send message.");
      input.value = "";
      showStatus("Message sent over radio.");
      await refresh();
      input.focus();
    } catch (error) { showStatus(error.message, true); }
    finally { button.disabled = false; }
  });

  document.querySelector("#name-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const nameInput = document.querySelector("#name-input");
    const response = await fetch("/name", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ username: nameInput.value })
    });
    const data = await response.json();
    if (!response.ok) showStatus(data.error || "Could not change name.", true);
    else showStatus(`Display name changed to ${data.username}.`);
  });

  refresh();
  window.setInterval(refresh, 1500);
})();
