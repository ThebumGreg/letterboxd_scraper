// static/js/payloads.js
document.addEventListener("DOMContentLoaded", function () {
    const tableBody = document.getElementById("payloadTableBody");
    const refreshBtn = document.getElementById("refreshLogsBtn");
    const clearBtn = document.getElementById("clearLogsBtn");
    const filters = document.querySelectorAll('input[name="trafficFilter"]');
    
    let loadedLogs = [];
    const payloadModal = new bootstrap.Modal(document.getElementById('payloadModal'));

    function formatJSON(str) {
        if (!str || str === "null" || str === "None") return "{}";
        try {
            const parsed = typeof str === "string" ? JSON.parse(str) : str;
            return JSON.stringify(parsed, null, 2);
        } catch (e) {
            return str;
        }
    }

    function renderTable() {
        const activeFilter = document.querySelector('input[name="trafficFilter"]:checked').id;
        
        let filteredLogs = loadedLogs;
        if (activeFilter === "filterWebhooks") {
            filteredLogs = loadedLogs.filter(log => log.direction === "IN");
        } else if (activeFilter === "filterErrors") {
            filteredLogs = loadedLogs.filter(log => log.status_code >= 400 || log.status_code === 0);
        }

        if (filteredLogs.length === 0) {
            tableBody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">No payloads matched the current filters.</td></tr>';
            return;
        }

        tableBody.innerHTML = filteredLogs.map(log => {
            const badgeClass = log.direction === "IN" ? "bg-info text-dark" : "bg-primary";
            let statusBadge = "bg-secondary";
            if (log.status_code >= 200 && log.status_code < 300) statusBadge = "bg-success";
            else if (log.status_code >= 400 || log.status_code === 0) statusBadge = "bg-danger";

            return `
                <tr>
                    <td class="font-monospace small">${log.created_at}</td>
                    <td><span class="badge ${badgeClass}">${log.direction}</span></td>
                    <td class="text-capitalize fw-bold">${log.service}</td>
                    <td>
                        <div class="fw-bold">${log.event_type}</div>
                        <div class="text-muted small text-truncate" style="max-width: 300px;" title="${log.endpoint}">${log.endpoint}</div>
                    </td>
                    <td><span class="badge ${statusBadge}">${log.status_code === 0 ? "ERR" : log.status_code}</span></td>
                    <td>${log.duration_ms} ms</td>
                    <td>
                        <button class="btn btn-sm btn-outline-dark view-payload-btn" data-id="${log.id}">
                            Inspect JSON
                        </button>
                    </td>
                </tr>
            `;
        }).join('');

        document.querySelectorAll('.view-payload-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const logId = parseInt(e.target.getAttribute('data-id'));
                const log = loadedLogs.find(l => l.id === logId);
                if (log) {
                    document.getElementById("payloadModalLabel").innerText = `${log.direction} | ${log.service.toUpperCase()} - ${log.event_type}`;
                    document.getElementById("headers-code").textContent = formatJSON(log.headers_json);
                    document.getElementById("request-code").textContent = formatJSON(log.payload_json);
                    document.getElementById("response-code").textContent = formatJSON(log.response_json);
                    payloadModal.show();
                }
            });
        });
    }

    async function fetchPayloads() {
        try {
            refreshBtn.innerHTML = '<i class="bi bi-hourglass-split"></i> Loading...';
            refreshBtn.disabled = true;
            
            const res = await fetch('/api/payload-logs?limit=500');
            if (res.ok) {
                loadedLogs = await res.json();
                renderTable();
            } else {
                console.error("Failed to fetch payload logs");
            }
        } catch (e) {
            console.error(e);
        } finally {
            refreshBtn.innerHTML = '<i class="bi bi-arrow-clockwise"></i> Refresh Now';
            refreshBtn.disabled = false;
        }
    }

    refreshBtn.addEventListener("click", fetchPayloads);
    
    filters.forEach(filter => {
        filter.addEventListener("change", renderTable);
    });

    clearBtn.addEventListener("click", async () => {
        if (confirm("Are you sure you want to clear all payload history?")) {
            const token = document.querySelector('meta[name="csrf-token"]')?.content || "";
            const res = await fetch('/api/payload-logs/clear', {
                method: 'POST',
                headers: token ? { 'X-CSRFToken': token } : {},
            });
            if (res.ok) {
                loadedLogs = [];
                renderTable();
            } else {
                alert("Could not clear the payload history (" + res.status + "). Reload the page and try again.");
            }
        }
    });

    // Export functionality
    const exportBtn = document.getElementById("exportLogsBtn");
    if (exportBtn) {
        exportBtn.addEventListener("click", () => {
            if (loadedLogs.length === 0) {
                alert("No payloads available to export.");
                return;
            }
            
            // Format the current logs into a downloadable JSON blob
            const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(loadedLogs, null, 2));
            const downloadAnchorNode = document.createElement('a');
            
            downloadAnchorNode.setAttribute("href", dataStr);
            downloadAnchorNode.setAttribute("download", "api_payloads_export.json");
            document.body.appendChild(downloadAnchorNode); // Required for Firefox support
            downloadAnchorNode.click();
            downloadAnchorNode.remove();
        });
    }

    // Copy to clipboard handlers
    document.querySelectorAll('.copy-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const targetId = e.target.getAttribute('data-target');
            const codeBlock = document.getElementById(targetId);
            navigator.clipboard.writeText(codeBlock.textContent).then(() => {
                const originalText = e.target.innerText;
                e.target.innerText = "Copied!";
                e.target.classList.replace('btn-outline-secondary', 'btn-success');
                setTimeout(() => {
                    e.target.innerText = originalText;
                    e.target.classList.replace('btn-success', 'btn-outline-secondary');
                }, 2000);
            });
        });
    });

    // Initial load
    fetchPayloads();
});