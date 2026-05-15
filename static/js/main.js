let currentEditTicker = null;

// Load holdings and refresh table
function loadHoldings() {
    fetch('/api/holdings')
        .then(r => r.json())
        .then(data => {
            renderHoldingsTable(data);
        })
        .catch(err => console.error(err));
}

function renderHoldingsTable(holdings) {
    const tbody = document.querySelector('#holdingsTable tbody');
    if (!tbody) return;
    
    tbody.innerHTML = '';

    holdings.forEach(h => {
        const pnlClass = (h.pnl_pct && h.pnl_pct > 0) ? 'positive' : 'negative';
        const pnlText = h.pnl_pct ? (h.pnl_pct * 100).toFixed(1) + '%' : '—';

        const row = `
            <tr>
                <td><strong>${h.ticker}</strong></td>
                <td>$${parseFloat(h.entry_price || 0).toFixed(2)}</td>
                <td>${h.shares || 0}</td>
                <td>$${parseFloat(h.current_price || 0).toFixed(2)}</td>
                <td class="${pnlClass}">${pnlText}</td>
                <td>$${parseFloat(h.stop_price || 0).toFixed(2)}</td>
                <td>${h.action || 'HOLD'}</td>
                <td>
                    <button class="btn btn-sm btn-warning me-1 edit-btn" data-ticker="${h.ticker}">
                        <i class="fas fa-edit"></i>
                    </button>
                    <button class="btn btn-sm btn-danger delete-btn" data-ticker="${h.ticker}">
                        <i class="fas fa-trash"></i>
                    </button>
                </td>
            </tr>
        `;
        tbody.innerHTML += row;
    });

    // Attach event listeners
    document.querySelectorAll('.edit-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            editHolding(this.dataset.ticker);
        });
    });

    document.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            if (confirm(`Delete ${this.dataset.ticker}?`)) {
                deleteHolding(this.dataset.ticker);
            }
        });
    });
}

// Add or Update Holding
function saveHolding() {
    const ticker = document.getElementById('ticker').value.toUpperCase().trim();
    const entry_price = parseFloat(document.getElementById('entry_price').value) || 0;
    const shares = parseFloat(document.getElementById('shares').value) || 0;

    if (!ticker || shares <= 0) {
        alert("Please enter valid Ticker and Shares");
        return;
    }

    fetch('/api/holdings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            ticker: ticker,
            entry_price: entry_price,
            shares: shares
        })
    })
    .then(r => r.json())
    .then(() => {
        bootstrap.Modal.getInstance(document.getElementById('addModal')).hide();
        loadHoldings();
    });
}

// Edit Holding
function editHolding(ticker) {
    fetch('/api/holdings')
        .then(r => r.json())
        .then(holdings => {
            const holding = holdings.find(h => h.ticker === ticker);
            if (holding) {
                document.getElementById('ticker').value = holding.ticker;
                document.getElementById('entry_price').value = holding.entry_price;
                document.getElementById('shares').value = holding.shares;
                document.getElementById('ticker').disabled = true; // can't change ticker easily
                
                const modal = new bootstrap.Modal(document.getElementById('addModal'));
                modal.show();
            }
        });
}

function deleteHolding(ticker) {
    fetch(`/api/holdings?ticker=${ticker}`, { method: 'DELETE' })
        .then(() => loadHoldings());
}

// Refresh portfolio + signals
function runPortfolioUpdate() {
    fetch('/api/run-portfolio')
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                alert('Portfolio updated! Refresh page to see changes.');
                location.reload();
            } else {
                alert('Error: ' + data.error);
            }
        })
        .catch(err => alert('Update failed: ' + err));
}

document.addEventListener('DOMContentLoaded', function() {
    // Delete buttons
    document.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            if (confirm('Delete holding?')) {
                const ticker = btn.dataset.ticker;
                fetch(`/api/holdings?ticker=${ticker}`, { method: 'DELETE' })
                    .then(() => location.reload());
            }
        });
    });
});