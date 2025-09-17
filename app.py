from flask import Flask, jsonify, request
from flask_cors import CORS
import logging
import time

# DEVELOPMENT MODE CONTROL
DEVELOPMENT_MODE = True  # Set False for production

# Import yfinance for real stock data
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    print("ERROR: yfinance not available! Install with: pip install yfinance")
    exit(1)

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global variables
pipeline_backtest = None
system_status = {"initialized": False, "training": False, "error": None}

# =================== YFINANCE DATA FETCHING ===================

def fetch_yfinance_data(ticker='TSLA', period='1y', interval='1d'):
    """Fetch real stock data from yfinance - NO MOCK DATA"""
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period, interval=interval)
        
        if hist.empty:
            logger.error(f"No data found for ticker {ticker}")
            return []
        
        candlestick_data = []
        for date, row in hist.iterrows():
            # Generate trading signals based on price action
            daily_change = (row['Close'] - row['Open']) / row['Open'] * 100
            signal = 'HOLD'
            if daily_change > 2:
                signal = 'BUY'
            elif daily_change < -2:
                signal = 'SELL'
            
            candlestick_data.append({
                'date': date.strftime('%Y-%m-%d'),
                'open': round(float(row['Open']), 2),
                'high': round(float(row['High']), 2),
                'low': round(float(row['Low']), 2),
                'close': round(float(row['Close']), 2),
                'volume': int(row['Volume']),
                'signal': signal,
                'change': round(daily_change, 2)
            })
        
        return candlestick_data
    
    except Exception as e:
        logger.error(f"Error fetching yfinance data: {e}")
        return []

def get_dummy_backtest_results():
    return {
        'total_return': 23.7,
        'sharpe_ratio': 1.45,
        'max_drawdown': -12.3,
        'win_rate': 68.4,
        'total_trades': 187,
        'profitable_trades': 128,
        'avg_trade_return': 1.2,
        'volatility': 18.5
    }

# =================== FLASK ROUTES ===================
@app.route('/')
def index():
    """Complete ACA Trading Pipeline with simplified cursor-following tooltips"""
    mode_indicator = "DEV" if DEVELOPMENT_MODE else "PROD"
    status_color = "#e74c3c" if DEVELOPMENT_MODE else "#27ae60"
    pipeline_status = f"Ready" if DEVELOPMENT_MODE else "ACA Pipeline Ready"
    
    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>ACA Trading Pipeline - Day-by-Day Animation</title>
        <meta charset="utf-8">
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ 
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
                background: #1e293b;
                color: #e2e8f0;
                min-height: 100vh;
            }}
            .container {{ 
                max-width: 1600px; 
                margin: 0 auto; 
                padding: 20px; 
            }}
            
            /* Header */
            .header {{ 
                text-align: center; 
                margin-bottom: 30px; 
            }}
            .header h1 {{ 
                font-size: 2.5em; 
                color: #f1f5f9; 
                margin-bottom: 10px;
                font-weight: 300;
            }}
            .mode-badge {{
                background: {status_color};
                color: white;
                padding: 4px 12px;
                border-radius: 12px;
                font-size: 0.8em;
                font-weight: 500;
                margin-left: 10px;
            }}
            
            /* Controls Section */
            .controls {{
                background: #334155;
                border-radius: 12px;
                padding: 20px;
                margin-bottom: 20px;
                display: grid;
                grid-template-columns: 1fr 1fr 1fr 1fr 2fr;
                gap: 15px;
                align-items: center;
            }}
            .control-group {{
                display: flex;
                flex-direction: column;
                gap: 8px;
            }}
            .control-group label {{
                font-size: 0.9em;
                color: #94a3b8;
                font-weight: 500;
            }}
            select, button {{
                background: #475569;
                border: 1px solid #64748b;
                color: #e2e8f0;
                padding: 12px 16px;
                border-radius: 8px;
                font-size: 1em;
                cursor: pointer;
                transition: all 0.3s ease;
            }}
            button:hover {{ background: #52525b; }}
            .btn-primary {{ background: #3b82f6; border-color: #3b82f6; }}
            .btn-primary:hover {{ background: #2563eb; }}
            .btn-success {{ background: #10b981; border-color: #10b981; }}
            .btn-success:hover {{ background: #059669; }}
            .btn-danger {{ background: #ef4444; border-color: #ef4444; }}
            .btn-danger:hover {{ background: #dc2626; }}
            
            /* Progress Section */
            .progress-section {{
                display: flex;
                flex-direction: column;
                gap: 8px;
            }}
            .progress-bar {{
                width: 100%;
                height: 8px;
                background: #475569;
                border-radius: 4px;
                overflow: hidden;
            }}
            .progress-fill {{
                height: 100%;
                background: linear-gradient(90deg, #3b82f6, #10b981);
                width: 0%;
                transition: width 0.3s ease;
            }}
            .progress-text {{
                font-size: 0.9em;
                color: #94a3b8;
            }}
            
            /* Status */
            .status {{
                text-align: center;
                padding: 15px;
                margin-bottom: 20px;
            }}
            .status-indicator {{
                display: inline-flex;
                align-items: center;
                gap: 8px;
                background: #374151;
                padding: 8px 16px;
                border-radius: 20px;
                font-weight: 500;
            }}
            .status-dot {{
                width: 8px;
                height: 8px;
                border-radius: 50%;
                background: {status_color};
            }}
            
            /* CUSTOM CANDLESTICK CHART */
            .chart-section {{
                background: #334155;
                border-radius: 12px;
                padding: 20px;
                margin-bottom: 20px;
            }}
            .chart-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 20px;
                padding-bottom: 15px;
                border-bottom: 2px solid #475569;
            }}
            .chart-title {{
                font-size: 1.8em;
                color: #f1f5f9;
                font-weight: 500;
            }}
            .chart-info {{
                color: #94a3b8;
                font-size: 0.9em;
            }}
            .chart-container {{
                position: relative;
                background: #1e293b;
                border-radius: 8px;
                margin-bottom: 20px;
                overflow-x: auto;
                overflow-y: hidden;
                max-width: 100%;
            }}
            #candlestickCanvas {{
                border-radius: 8px;
                cursor: crosshair;
                display: block;
            }}
            
            /* Simplified Tooltip */
            .tooltip {{
                position: fixed;
                background: rgba(0, 0, 0, 0.9);
                color: white;
                padding: 12px;
                border-radius: 8px;
                font-size: 0.9em;
                pointer-events: none;
                z-index: 1000;
                display: none;
                border: 1px solid #475569;
                max-width: 200px;
            }}
            .tooltip-row {{
                display: flex;
                justify-content: space-between;
                margin: 2px 0;
                min-width: 120px;
            }}
            .tooltip-label {{
                color: #94a3b8;
            }}
            .tooltip-value {{
                color: #f1f5f9;
                font-weight: 600;
            }}
            .positive {{ color: #10b981; }}
            .negative {{ color: #ef4444; }}
            .buy {{ color: #10b981; font-weight: bold; }}
            .sell {{ color: #ef4444; font-weight: bold; }}
            .hold {{ color: #94a3b8; }}
            
            /* Portfolio Section */
            .portfolio-section {{
                background: #334155;
                border-radius: 12px;
                padding: 25px;
                margin-bottom: 20px;
            }}
            .portfolio-container {{
                display: grid;
                grid-template-columns: 2fr 1fr;
                gap: 20px;
            }}
            .portfolio-display {{
                background: #475569;
                border-radius: 10px;
                padding: 20px;
            }}
            .portfolio-input {{
                background: #475569;
                border-radius: 10px;
                padding: 20px;
            }}
            
            /* Results Section */
            .results-section {{
                margin-top: 20px;
                padding: 20px;
                background: #1f2937;
                border-radius: 12px;
                border-left: 4px solid #3b82f6;
                display: none;
            }}
            .results-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 15px;
                margin-top: 15px;
            }}
            .metric {{
                background: #475569;
                padding: 15px;
                border-radius: 8px;
                text-align: center;
            }}
            .metric-value {{
                font-size: 1.4em;
                font-weight: 600;
                color: #f1f5f9;
                margin-bottom: 5px;
            }}
            .metric-label {{
                font-size: 0.8em;
                color: #94a3b8;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <!-- Header -->
            <div class="header">
                <h1>📈 ACA Trading Pipeline<span class="mode-badge">{mode_indicator}</span></h1>
            </div>
            
            <!-- Controls -->
            <div class="controls">
                <div class="control-group">
                    <label>📈 Stock</label>
                    <select id="stockSelect">
                        <option value="TSLA">TSLA</option>
                        <option value="AAPL">AAPL</option>
                        <option value="GOOGL">GOOGL</option>
                        <option value="MSFT">MSFT</option>
                        <option value="NVDA">NVDA</option>
                    </select>
                </div>
                
                <div class="control-group">
                    <label>🚀 Actions</label>
                    <button id="runBacktest" class="btn-primary" onclick="runBacktest()">
                        🔬 Run Backtest
                    </button>
                </div>
                
                <div class="control-group">
                    <label>▶️ Playback</label>
                    <button id="playBtn" class="btn-success" onclick="togglePlay()">
                        ▶️ Play
                    </button>
                </div>
                
                <div class="control-group">
                    <label>🔄 Reset</label>
                    <button id="resetBtn" class="btn-danger" onclick="resetSystem()">
                        🔄 Reset
                    </button>
                </div>
                
                <div class="control-group progress-section">
                    <label>⚡ Speed & 📊 Progress</label>
                    <div class="progress-bar">
                        <div class="progress-fill" id="progressFill"></div>
                    </div>
                    <div class="progress-text">
                        <span id="progressText">0/0</span> • 
                        <span id="speedText">100ms</span>
                    </div>
                    <input type="range" id="speedSlider" min="50" max="500" value="100" step="50" 
                           style="width: 100%; margin-top: 5px;">
                </div>
            </div>
            
            <!-- Status -->
            <div class="status">
                <div class="status-indicator">
                    <div class="status-dot"></div>
                    <span id="systemStatus">{pipeline_status}</span>
                </div>
            </div>
            
            <!-- CANDLESTICK CHART SECTION -->
            <div class="chart-section">
                <div class="chart-header">
                    <div class="chart-title">📊 <span id="chartSymbol">TSLA</span> Day-by-Day Analysis</div>
                    <div class="chart-info" id="chartInfo">Run backtest first, then click Play to see day-by-day animation</div>
                </div>
                
                <!-- Chart Container -->
                <div class="chart-container" id="chartContainer">
                    <canvas id="candlestickCanvas" width="1200" height="500"></canvas>
                    <div class="tooltip" id="tooltip">
                        <div class="tooltip-row">
                            <span class="tooltip-label">Date:</span>
                            <span class="tooltip-value" id="tooltipDate">-</span>
                        </div>
                        <div class="tooltip-row">
                            <span class="tooltip-label">Open:</span>
                            <span class="tooltip-value" id="tooltipOpen">$0.00</span>
                        </div>
                        <div class="tooltip-row">
                            <span class="tooltip-label">High:</span>
                            <span class="tooltip-value" id="tooltipHigh">$0.00</span>
                        </div>
                        <div class="tooltip-row">
                            <span class="tooltip-label">Low:</span>
                            <span class="tooltip-value" id="tooltipLow">$0.00</span>
                        </div>
                        <div class="tooltip-row">
                            <span class="tooltip-label">Close:</span>
                            <span class="tooltip-value" id="tooltipClose">$0.00</span>
                        </div>
                        <div class="tooltip-row">
                            <span class="tooltip-label">Change:</span>
                            <span class="tooltip-value" id="tooltipChange">+0.0%</span>
                        </div>
                        <div class="tooltip-row">
                            <span class="tooltip-label">Signal:</span>
                            <span class="tooltip-value" id="tooltipSignal">HOLD</span>
                        </div>
                        <div class="tooltip-row">
                            <span class="tooltip-label">Volume:</span>
                            <span class="tooltip-value" id="tooltipVolume">0</span>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Enhanced Portfolio Section -->
            <div class="portfolio-section">
                <div class="chart-header">
                    <div class="chart-title">💼 Portfolio Management</div>
                </div>
                
                <div class="portfolio-container">
                    <!-- Left: Portfolio Holdings Display -->
                    <div class="portfolio-display">
                        <h4 style="margin-bottom: 15px; color: #f1f5f9;">Current Holdings</h4>
                        
                        <!-- Portfolio Summary Cards -->
                        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; margin-bottom: 20px;">
                            <div style="background: #1e293b; padding: 15px; border-radius: 8px; text-align: center;">
                                <div id="totalValue" style="font-size: 1.5em; font-weight: 600; color: #10b981;">$0.00</div>
                                <div style="color: #94a3b8; font-size: 0.9em;">Total Value</div>
                            </div>
                            <div style="background: #1e293b; padding: 15px; border-radius: 8px; text-align: center;">
                                <div id="totalGainLoss" style="font-size: 1.5em; font-weight: 600; color: #94a3b8;">$0.00</div>
                                <div style="color: #94a3b8; font-size: 0.9em;">Total Gain/Loss</div>
                            </div>
                            <div style="background: #1e293b; padding: 15px; border-radius: 8px; text-align: center;">
                                <div id="totalGainLossPercent" style="font-size: 1.5em; font-weight: 600; color: #94a3b8;">0.0%</div>
                                <div style="color: #94a3b8; font-size: 0.9em;">Total Return %</div>
                            </div>
                        </div>
                        
                        <!-- Holdings Table -->
                        <table style="width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 8px; overflow: hidden;">
                            <thead>
                                <tr style="background: #374151;">
                                    <th style="padding: 12px; text-align: left; color: #f1f5f9; font-weight: 600;">Symbol</th>
                                    <th style="padding: 12px; text-align: right; color: #f1f5f9; font-weight: 600;">Qty</th>
                                    <th style="padding: 12px; text-align: right; color: #f1f5f9; font-weight: 600;">Avg Price</th>
                                    <th style="padding: 12px; text-align: right; color: #f1f5f9; font-weight: 600;">Current</th>
                                    <th style="padding: 12px; text-align: right; color: #f1f5f9; font-weight: 600;">Value</th>
                                    <th style="padding: 12px; text-align: right; color: #f1f5f9; font-weight: 600;">Gain/Loss</th>
                                    <th style="padding: 12px; text-align: right; color: #f1f5f9; font-weight: 600;">%</th>
                                </tr>
                            </thead>
                            <tbody id="portfolioTableBody">
                                <tr>
                                    <td colspan="7" style="padding: 20px; text-align: center; color: #94a3b8;">
                                        No holdings yet. Add stocks using the form →
                                    </td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                    
                    <!-- Right: Add/Update Stock Form -->
                    <div class="portfolio-input">
                        <h4 style="margin-bottom: 15px; color: #f1f5f9;">Add/Update Stock</h4>
                        
                        <form id="portfolioForm" style="display: flex; flex-direction: column; gap: 15px;">
                            <div>
                                <label style="display: block; margin-bottom: 5px; color: #94a3b8; font-size: 0.9em;">Stock Symbol</label>
                                <input type="text" id="stockSymbol" name="stockSymbol" placeholder="TSLA" required 
                                       style="width: 100%; padding: 10px; border-radius: 6px; border: 1px solid #64748b; background: #1e293b; color: #e2e8f0;">
                            </div>
                            
                            <div>
                                <label style="display: block; margin-bottom: 5px; color: #94a3b8; font-size: 0.9em;">Average Price</label>
                                <input type="number" step="0.01" id="avgPrice" name="avgPrice" placeholder="350.00" required
                                       style="width: 100%; padding: 10px; border-radius: 6px; border: 1px solid #64748b; background: #1e293b; color: #e2e8f0;">
                            </div>
                            
                            <div>
                                <label style="display: block; margin-bottom: 5px; color: #94a3b8; font-size: 0.9em;">Quantity</label>
                                <input type="number" id="quantity" name="quantity" placeholder="100" required
                                       style="width: 100%; padding: 10px; border-radius: 6px; border: 1px solid #64748b; background: #1e293b; color: #e2e8f0;">
                            </div>
                            
                            <button type="submit" style="background: #3b82f6; color: white; padding: 12px; border-radius: 6px; border: none; cursor: pointer; font-weight: 600; transition: background 0.3s ease;"
                                    onmouseover="this.style.background='#2563eb'" onmouseout="this.style.background='#3b82f6'">
                                Add/Update Stock
                            </button>
                            
                            <button type="button" id="clearPortfolio" style="background: #ef4444; color: white; padding: 8px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.9em;"
                                    onmouseover="this.style.background='#dc2626'" onmouseout="this.style.background='#ef4444'">
                                Clear All
                            </button>
                        </form>
                        
                        <div style="margin-top: 20px; padding: 15px; background: #1e293b; border-radius: 8px; border-left: 4px solid #3b82f6;">
                            <h5 style="color: #f1f5f9; margin-bottom: 10px;">💡 Tips:</h5>
                            <ul style="color: #94a3b8; font-size: 0.85em; margin: 0; padding-left: 15px;">
                                <li>Enter your actual purchase price and quantity</li>
                                <li>Current prices update automatically</li>
                                <li>Re-enter same symbol to update existing holding</li>
                            </ul>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Results Section -->
            <div class="results-section" id="resultsSection">
                <h3>📊 Backtest Results</h3>
                <div class="results-grid" id="resultsGrid">
                    <!-- Results populated by JavaScript -->
                </div>
            </div>
        </div>
        
        <script>
            // Global variables
            let canvas, ctx;
            let chartData = [];
            let isPlaying = false;
            let isRunning = false;
            let currentDay = 0;
            let totalDays = 0;
            let playSpeed = 1000;
            let playInterval = null;
            let candleWidth = 12;
            let candleSpacing = 4;
            let maxVisibleCandles = 60;
            let chartPadding = {{ left: 80, right: 50, top: 50, bottom: 50 }};
            let portfolioData = [];
            let minPrice, maxPrice; // Global price range variables
            
            // Initialize
            document.addEventListener('DOMContentLoaded', function() {{
                initializeChart();
                setupEventListeners();
                renderPortfolioTable();
            }});
            
            function initializeChart() {{
                canvas = document.getElementById('candlestickCanvas');
                ctx = canvas.getContext('2d');
                
                // Set canvas size
                const container = document.getElementById('chartContainer');
                canvas.width = container.clientWidth || 1200;
                canvas.height = 500;
                
                // Draw initial empty chart
                drawChart();
            }}
            
            function setupEventListeners() {{
                // SIMPLIFIED: Mouse events for tooltip - cursor following approach
                canvas.addEventListener('mousemove', handleSimpleMouseMove);
                canvas.addEventListener('mouseleave', hideTooltip);
                
                // Speed slider
                document.getElementById('speedSlider').addEventListener('input', function() {{
                    playSpeed = parseInt(this.value);
                    document.getElementById('speedText').textContent = playSpeed + 'ms';
                    
                    if (isPlaying) {{
                        clearInterval(playInterval);
                        playInterval = setInterval(playStep, playSpeed);
                    }}
                }});
                
                // Window resize
                window.addEventListener('resize', function() {{
                    const container = document.getElementById('chartContainer');
                    canvas.width = container.clientWidth || 1200;
                    drawChart();
                }});
            }}
            
            // SIMPLIFIED: Mouse move handler for cursor-following tooltip
            function handleSimpleMouseMove(event) {{
                if (currentDay === 0 || chartData.length === 0) {{
                    hideTooltip();
                    return;
                }}
                
                const mouseX = event.offsetX;
                const mouseY = event.offsetY;
                
                const visibleCount = Math.min(currentDay, maxVisibleCandles);
                const startIndex = currentDay > maxVisibleCandles ? currentDay - maxVisibleCandles : 0;
                
                // Check if mouse is over any candlestick
                for (let i = 0; i < visibleCount; i++) {{
                    const candleStartX = chartPadding.left + 20 + i * (candleWidth + candleSpacing);
                    const candleEndX = candleStartX + candleWidth;
                    
                    if (mouseX >= candleStartX && mouseX <= candleEndX) {{
                        const candle = chartData[startIndex + i];
                        
                        // Calculate Y positions for this candle
                        const priceToY = (price) => chartPadding.top + (canvas.height - chartPadding.top - chartPadding.bottom) * 
                                                   (1 - (price - minPrice) / (maxPrice - minPrice));
                        const candleTopY = priceToY(candle.high);
                        const candleBottomY = priceToY(candle.low);
                        
                        // Check if mouse is within candle vertical bounds
                        if (mouseY >= candleTopY && mouseY <= candleBottomY) {{
                            // Show tooltip near cursor (15px right, 15px up)
                            showSimpleTooltip(candle, event.clientX + 15, event.clientY - 15);
                            return;
                        }}
                    }}
                }}
                
                // Not over any candle
                hideTooltip();
            }}
            
            // SIMPLIFIED: Show tooltip with simple cursor positioning
            function showSimpleTooltip(candle, x, y) {{
                const tooltip = document.getElementById('tooltip');
                
                // Update tooltip content
                document.getElementById('tooltipDate').textContent = candle.date;
                document.getElementById('tooltipOpen').textContent = '$' + candle.open.toFixed(2);
                document.getElementById('tooltipHigh').textContent = '$' + candle.high.toFixed(2);
                document.getElementById('tooltipLow').textContent = '$' + candle.low.toFixed(2);
                document.getElementById('tooltipClose').textContent = '$' + candle.close.toFixed(2);
                
                const changeElement = document.getElementById('tooltipChange');
                changeElement.textContent = (candle.change >= 0 ? '+' : '') + candle.change.toFixed(2) + '%';
                changeElement.className = 'tooltip-value ' + (candle.change >= 0 ? 'positive' : 'negative');
                
                const signalElement = document.getElementById('tooltipSignal');
                signalElement.textContent = candle.signal;
                signalElement.className = 'tooltip-value ' + candle.signal.toLowerCase();
                
                document.getElementById('tooltipVolume').textContent = candle.volume.toLocaleString();
                
                // Simple positioning near cursor
                tooltip.style.left = x + 'px';
                tooltip.style.top = y + 'px';
                tooltip.style.display = 'block';
            }}
            
            function hideTooltip() {{
                document.getElementById('tooltip').style.display = 'none';
            }}
            
            function adjustCanvasWidth() {{
                if (chartData.length === 0) return;
                
                // Calculate total width needed for all candlesticks
                const totalCandlesWidth = chartData.length * (candleWidth + candleSpacing);
                const requiredWidth = chartPadding.left + chartPadding.right + totalCandlesWidth + 40;
                
                // Set canvas width to accommodate all data
                const container = document.getElementById('chartContainer');
                const minWidth = container.clientWidth || 1200;
                
                canvas.width = Math.max(minWidth, requiredWidth);
                canvas.height = 500;
                
                // Redraw chart with new dimensions
                drawChart();
            }}
            
            function drawChart() {{
                // Clear canvas
                ctx.fillStyle = '#1e293b';
                ctx.fillRect(0, 0, canvas.width, canvas.height);
                
                if (chartData.length === 0) {{
                    drawEmptyChart();
                    return;
                }}
                
                const chartWidth = canvas.width - chartPadding.left - chartPadding.right;
                const chartHeight = canvas.height - chartPadding.top - chartPadding.bottom;
                
                // Calculate how many candles can fit with fixed spacing
                const candleAreaWidth = candleWidth + candleSpacing;
                maxVisibleCandles = Math.floor(chartWidth / candleAreaWidth);
                
                // Determine which candles to show
                let startIndex = 0;
                let endIndex = currentDay;
                
                // If we have more candles than can fit, slide the window
                if (currentDay > maxVisibleCandles) {{
                    startIndex = currentDay - maxVisibleCandles;
                    endIndex = currentDay;
                }}
                
                const visibleData = chartData.slice(startIndex, endIndex);
                
                if (visibleData.length === 0) {{
                    drawEmptyChart();
                    drawGrid(chartWidth, chartHeight);
                    return;
                }}
                
                // Calculate price range from ALL data (for consistent scaling)
                minPrice = Math.min(...chartData.map(d => d.low));
                maxPrice = Math.max(...chartData.map(d => d.high));
                const priceRange = maxPrice - minPrice;
                const padding = priceRange * 0.1;
                minPrice -= padding;
                maxPrice += padding;
                
                // Draw grid
                drawGrid(chartWidth, chartHeight, minPrice, maxPrice);
                
                // Draw candlesticks with fixed spacing from left to right
                visibleData.forEach((candle, index) => {{
                    drawCandlestick(candle, index, minPrice, maxPrice, chartWidth, chartHeight);
                }});
                
                // Draw current day indicator (always on the rightmost candle)
                if (currentDay > 0 && visibleData.length > 0) {{
                    drawCurrentDayIndicator(visibleData.length - 1, chartHeight);
                }}
            }}
            
            function drawEmptyChart() {{
                ctx.fillStyle = '#94a3b8';
                ctx.font = '16px Arial';
                ctx.textAlign = 'center';
                const message = chartData.length === 0 
                    ? 'Click "Run Backtest" to load data' 
                    : 'Click "Play" to start day-by-day animation';
                ctx.fillText(message, canvas.width / 2, canvas.height / 2);
            }}
            
            function drawGrid(chartWidth, chartHeight, minPrice = 0, maxPrice = 100) {{
                ctx.strokeStyle = '#374151';
                ctx.lineWidth = 1;
                
                // Horizontal grid lines (price levels)
                const priceSteps = 5;
                for (let i = 0; i <= priceSteps; i++) {{
                    const price = minPrice + (maxPrice - minPrice) * i / priceSteps;
                    const y = chartPadding.top + chartHeight - (price - minPrice) / (maxPrice - minPrice) * chartHeight;
                    
                    ctx.beginPath();
                    ctx.moveTo(chartPadding.left, y);
                    ctx.lineTo(chartPadding.left + chartWidth, y);
                    ctx.stroke();
                    
                    // Price labels
                    ctx.fillStyle = '#94a3b8';
                    ctx.font = '12px Arial';
                    ctx.textAlign = 'right';
                    ctx.fillText('$' + price.toFixed(2), chartPadding.left - 10, y + 4);
                }}
                
                // Vertical grid lines
                const timeSteps = Math.min(10, Math.floor(maxVisibleCandles / 5));
                for (let i = 0; i <= timeSteps; i++) {{
                    const x = chartPadding.left + chartWidth * i / timeSteps;
                    
                    ctx.beginPath();
                    ctx.moveTo(x, chartPadding.top);
                    ctx.lineTo(x, chartPadding.top + chartHeight);
                    ctx.stroke();
                }}
            }}
            
            function drawCandlestick(candle, index, minPrice, maxPrice, chartWidth, chartHeight) {{
                // Fixed spacing - candles appear left to right with consistent gaps
                const x = chartPadding.left + 20 + index * (candleWidth + candleSpacing) + candleWidth / 2;
                const priceToY = (price) => chartPadding.top + chartHeight - (price - minPrice) / (maxPrice - minPrice) * chartHeight;
                
                const openY = priceToY(candle.open);
                const closeY = priceToY(candle.close);
                const highY = priceToY(candle.high);
                const lowY = priceToY(candle.low);
                
                const isGreen = candle.close > candle.open;
                const color = isGreen ? '#10b981' : '#ef4444';
                
                // Draw wick (gray for better visibility)
                ctx.strokeStyle = '#666666';
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(x, highY);
                ctx.lineTo(x, lowY);
                ctx.stroke();
                
                // Draw body - FULLY FILLED for both green and red
                const bodyTop = Math.min(openY, closeY);
                const bodyHeight = Math.abs(closeY - openY);
                
                // Always fill the candlestick body
                ctx.fillStyle = color;
                ctx.fillRect(x - candleWidth / 2, bodyTop, candleWidth, Math.max(bodyHeight, 1));
                
                // Draw border for definition
                ctx.strokeStyle = '#333333';
                ctx.lineWidth = 1;
                ctx.strokeRect(x - candleWidth / 2, bodyTop, candleWidth, Math.max(bodyHeight, 1));
                
                // Draw Buy/Sell markers - dots at exact price levels, text above candle
                if (candle.signal === 'BUY') {{
                    // Green dot at the BUY price (low)
                    ctx.fillStyle = '#10b981';
                    ctx.beginPath();
                    ctx.arc(x, lowY, 4, 0, 2 * Math.PI);
                    ctx.fill();
                    
                    // BUY text above the candle
                    ctx.fillStyle = '#10b981';
                    ctx.font = 'bold 10px Arial';
                    ctx.textAlign = 'center';
                    ctx.fillText('BUY', x, highY - 15);
                    
                }} else if (candle.signal === 'SELL') {{
                    // Red dot at the SELL price (high)
                    ctx.fillStyle = '#ef4444';
                    ctx.beginPath();
                    ctx.arc(x, highY, 4, 0, 2 * Math.PI);
                    ctx.fill();
                    
                    // SELL text above the candle
                    ctx.fillStyle = '#ef4444';
                    ctx.font = 'bold 10px Arial';
                    ctx.textAlign = 'center';
                    ctx.fillText('SELL', x, highY - 15);
                }}
            }}
            
            function drawCurrentDayIndicator(candleIndex, chartHeight) {{
                // Current day indicator on the rightmost visible candle
                const x = chartPadding.left + 20 + candleIndex * (candleWidth + candleSpacing) + candleWidth / 2;
                
                ctx.strokeStyle = '#3b82f6';
                ctx.lineWidth = 2;
                ctx.setLineDash([5, 5]);
                ctx.beginPath();
                ctx.moveTo(x, chartPadding.top);
                ctx.lineTo(x, chartPadding.top + chartHeight);
                ctx.stroke();
                ctx.setLineDash([]);
            }}
            
            async function runBacktest() {{
                if (isRunning) return;
                
                const button = document.getElementById('runBacktest');
                const stock = document.getElementById('stockSelect').value;
                
                isRunning = true;
                button.disabled = true;
                button.textContent = '🔄 Running...';
                
                try {{
                    // Fetch candlestick data from yfinance
                    updateSystemStatus('📊 Fetching ' + stock + ' data from Yahoo Finance...');
                    const response = await fetch('/api/stock-data/' + stock);
                    const data = await response.json();
                    
                    if (data.length === 0) {{
                        throw new Error('No data received for ' + stock);
                    }}
                    
                    chartData = data;
                    totalDays = chartData.length;
                    currentDay = 0;
                    
                    // Update chart title
                    document.getElementById('chartSymbol').textContent = stock;
                    
                    // Adjust canvas width for scrolling
                    adjustCanvasWidth();
                    
                    // Call backtest API
                    const backtestResponse = await fetch('/api/run-backtest', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{stock: stock}})
                    }});
                    
                    const backtestData = await backtestResponse.json();
                    
                    if (backtestData.status === 'success') {{
                        displayResults(backtestData.results, stock);
                        updateSystemStatus('✅ Data Loaded - Click Play for Day-by-Day Animation');
                        updateChartInfo('Ready for day-by-day animation - ' + totalDays + ' trading days loaded');
                    }} else {{
                        throw new Error(backtestData.message);
                    }}
                    
                }} catch (error) {{
                    console.error('Backtest error:', error);
                    updateSystemStatus('❌ Error: ' + error.message);
                }} finally {{
                    isRunning = false;
                    button.disabled = false;
                    button.textContent = '🔬 Run Backtest';
                }}
            }}
            
            function togglePlay() {{
                const button = document.getElementById('playBtn');
                
                if (!chartData.length) {{
                    alert('Please run backtest first to load data!');
                    return;
                }}
                
                if (!isPlaying) {{
                    isPlaying = true;
                    button.textContent = '⏸️ Pause';
                    button.className = 'btn-danger';
                    playInterval = setInterval(playStep, playSpeed);
                    updateSystemStatus('▶️ Playing Day-by-Day Animation');
                }} else {{
                    isPlaying = false;
                    button.textContent = '▶️ Play';
                    button.className = 'btn-success';
                    clearInterval(playInterval);
                    updateSystemStatus('⏸️ Animation Paused');
                }}
            }}
            
            function playStep() {{
                if (currentDay >= totalDays) {{
                    isPlaying = false;
                    const button = document.getElementById('playBtn');
                    button.textContent = '▶️ Play';
                    button.className = 'btn-success';
                    clearInterval(playInterval);
                    updateSystemStatus('🏁 Animation Complete - All ' + totalDays + ' days shown');
                    return;
                }}
                
                currentDay++;
                updateProgress(currentDay, totalDays);
                
                // Redraw chart with new candle
                drawChart();
                
                // Auto-scroll to follow the animation
                const container = document.getElementById('chartContainer');
                if (currentDay > maxVisibleCandles) {{
                    const scrollPosition = (currentDay - maxVisibleCandles) * (candleWidth + candleSpacing);
                    container.scrollLeft = scrollPosition;
                }}
                
                // Update info
                const dayData = chartData[currentDay - 1];
                updateChartInfo(`Day ${{currentDay}} - ${{dayData.date}} - Signal: ${{dayData.signal}} - Price: $${{dayData.close}}`);
            }}
            
            function resetSystem() {{
                if (isPlaying) {{
                    togglePlay();
                }}
                
                currentDay = 0;
                
                // Reset scroll position
                const container = document.getElementById('chartContainer');
                container.scrollLeft = 0;
                
                drawChart();
                
                document.getElementById('resultsSection').style.display = 'none';
                updateProgress(0, 0);
                updateSystemStatus('{pipeline_status}');
                updateChartInfo('Run backtest first, then click Play to see day-by-day animation');
            }}
            
            function displayResults(results, stock) {{
                const resultsSection = document.getElementById('resultsSection');
                const resultsGrid = document.getElementById('resultsGrid');
                
                resultsGrid.innerHTML = `
                    <div class="metric">
                        <div class="metric-value positive">${{results.total_return.toFixed(1)}}%</div>
                        <div class="metric-label">Total Return</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value">${{results.sharpe_ratio.toFixed(2)}}</div>
                        <div class="metric-label">Sharpe Ratio</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value negative">${{results.max_drawdown.toFixed(1)}}%</div>
                        <div class="metric-label">Max Drawdown</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value">${{results.win_rate.toFixed(1)}}%</div>
                        <div class="metric-label">Win Rate</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value">${{results.total_trades}}</div>
                        <div class="metric-label">Total Trades</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value positive">${{results.profitable_trades}}</div>
                        <div class="metric-label">Profitable Trades</div>
                    </div>
                `;
                
                resultsSection.style.display = 'block';
            }}
            
            // Portfolio Management Functions
            function renderPortfolioTable() {{
                const tbody = document.getElementById('portfolioTableBody');
                
                if (portfolioData.length === 0) {{
                    tbody.innerHTML = `
                        <tr>
                            <td colspan="7" style="padding: 20px; text-align: center; color: #94a3b8;">
                                No holdings yet. Add stocks using the form →
                            </td>
                        </tr>
                    `;
                    updatePortfolioSummary();
                    return;
                }}
                
                tbody.innerHTML = '';
                portfolioData.forEach((stock, index) => {{
                    const currentValue = stock.currentPrice * stock.quantity;
                    const costBasis = stock.avgPrice * stock.quantity;
                    const gainLoss = currentValue - costBasis;
                    const gainLossPercent = (gainLoss / costBasis) * 100;
                    
                    const row = document.createElement('tr');
                    row.style.borderBottom = '1px solid #475569';
                    row.style.cursor = 'pointer';
                    row.onmouseover = () => row.style.background = '#374151';
                    row.onmouseout = () => row.style.background = 'transparent';
                    
                    row.innerHTML = `
                        <td style="padding: 12px; color: #3b82f6; font-weight: 600;">${{stock.symbol}}</td>
                        <td style="padding: 12px; text-align: right;">${{stock.quantity}}</td>
                        <td style="padding: 12px; text-align: right;">$${{stock.avgPrice.toFixed(2)}}</td>
                        <td style="padding: 12px; text-align: right;">$${{(stock.currentPrice || 0).toFixed(2)}}</td>
                        <td style="padding: 12px; text-align: right;">$${{currentValue.toFixed(2)}}</td>
                        <td style="padding: 12px; text-align: right; color: ${{gainLoss >= 0 ? '#10b981' : '#ef4444'}};">
                            $${{gainLoss.toFixed(2)}}
                        </td>
                        <td style="padding: 12px; text-align: right; color: ${{gainLossPercent >= 0 ? '#10b981' : '#ef4444'}};">
                            ${{gainLossPercent.toFixed(1)}}%
                        </td>
                    `;
                    
                    tbody.appendChild(row);
                }});
                
                updatePortfolioSummary();
            }}
            
            function updatePortfolioSummary() {{
                const totalValue = portfolioData.reduce((sum, stock) => sum + (stock.currentPrice || 0) * stock.quantity, 0);
                const totalCost = portfolioData.reduce((sum, stock) => sum + stock.avgPrice * stock.quantity, 0);
                const totalGainLoss = totalValue - totalCost;
                const totalGainLossPercent = totalCost > 0 ? (totalGainLoss / totalCost) * 100 : 0;
                
                document.getElementById('totalValue').textContent = `$${{totalValue.toFixed(2)}}`;
                document.getElementById('totalValue').style.color = totalValue > 0 ? '#10b981' : '#94a3b8';
                
                const gainLossElement = document.getElementById('totalGainLoss');
                gainLossElement.textContent = `${{totalGainLoss >= 0 ? '+' : ''}}$${{totalGainLoss.toFixed(2)}}`;
                gainLossElement.style.color = totalGainLoss >= 0 ? '#10b981' : '#ef4444';
                
                const percentElement = document.getElementById('totalGainLossPercent');
                percentElement.textContent = `${{totalGainLossPercent >= 0 ? '+' : ''}}${{totalGainLossPercent.toFixed(1)}}%`;
                percentElement.style.color = totalGainLossPercent >= 0 ? '#10b981' : '#ef4444';
            }}
            
            async function fetchCurrentPrice(symbol) {{
                try {{
                    const response = await fetch(`/api/stock-price/${{symbol}}`);
                    const data = await response.json();
                    return data.price || 0;
                }} catch (error) {{
                    console.error(`Error fetching price for ${{symbol}}:`, error);
                    return 0;
                }}
            }}
            
            // Portfolio form submission
            document.getElementById('portfolioForm').addEventListener('submit', async function(e) {{
                e.preventDefault();
                
                const symbol = document.getElementById('stockSymbol').value.toUpperCase().trim();
                const avgPrice = parseFloat(document.getElementById('avgPrice').value);
                const quantity = parseInt(document.getElementById('quantity').value);
                
                if (!symbol || avgPrice <= 0 || quantity <= 0) {{
                    alert('Please enter valid values');
                    return;
                }}
                
                // Show loading
                const submitBtn = this.querySelector('button[type="submit"]');
                const originalText = submitBtn.textContent;
                submitBtn.textContent = 'Loading...';
                submitBtn.disabled = true;
                
                try {{
                    // Fetch current price
                    const currentPrice = await fetchCurrentPrice(symbol);
                    
                    // Check if stock already exists
                    const existingIndex = portfolioData.findIndex(s => s.symbol === symbol);
                    if (existingIndex !== -1) {{
                        // Update existing
                        portfolioData[existingIndex] = {{ symbol, avgPrice, quantity, currentPrice }};
                    }} else {{
                        // Add new
                        portfolioData.push({{ symbol, avgPrice, quantity, currentPrice }});
                    }}
                    
                    renderPortfolioTable();
                    this.reset();
                }} catch (error) {{
                    alert('Error adding stock: ' + error.message);
                }} finally {{
                    submitBtn.textContent = originalText;
                    submitBtn.disabled = false;
                }}
            }});
            
            // Clear portfolio
            document.getElementById('clearPortfolio').addEventListener('click', function() {{
                if (confirm('Are you sure you want to clear all holdings?')) {{
                    portfolioData = [];
                    renderPortfolioTable();
                }}
            }});
            
            // Update current prices periodically
            setInterval(async () => {{
                if (portfolioData.length > 0) {{
                    for (let stock of portfolioData) {{
                        stock.currentPrice = await fetchCurrentPrice(stock.symbol);
                    }}
                    renderPortfolioTable();
                }}
            }}, 300000); // Update every 5 minutes
            
            function updateProgress(current, total) {{
                const progressFill = document.getElementById('progressFill');
                const progressText = document.getElementById('progressText');
                
                const percentage = total > 0 ? (current / total) * 100 : 0;
                progressFill.style.width = percentage + '%';
                progressText.textContent = current + '/' + total;
            }}
            
            function updateSystemStatus(status) {{
                document.getElementById('systemStatus').textContent = status;
            }}
            
            function updateChartInfo(info) {{
                document.getElementById('chartInfo').textContent = info;
            }}
        </script>
    </body>
    </html>
    '''

@app.route('/api/stock-data/<ticker>')
def api_stock_data(ticker):
    """API endpoint for real stock data from yfinance"""
    try:
        data = fetch_yfinance_data(ticker, period='1y', interval='1d')
        if not data:
            return jsonify({"error": f"No data found for {ticker}"}), 404
        return jsonify(data)
    except Exception as e:
        logger.error(f"Error fetching stock data: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stock-price/<symbol>')
def get_current_price(symbol):
    """Get current price for portfolio calculation"""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1d")
        if not hist.empty:
            current_price = float(hist['Close'].iloc[-1])
            return jsonify({"price": current_price})
        else:
            return jsonify({"error": "No data found"}), 404
    except Exception as e:
        logger.error(f"Error fetching price for {symbol}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/run-backtest', methods=['POST'])
def run_backtest():
    """Run backtest API"""
    try:
        data = request.get_json() or {}
        stock = data.get('stock', 'TSLA')
        
        if DEVELOPMENT_MODE:
            logger.info(f"[DEV] Running mock backtest for {stock}")
            time.sleep(2)
            results = get_dummy_backtest_results()
            
            # Customize results based on stock
            if stock == 'TSLA':
                results['total_return'] = 23.7
                results['win_rate'] = 68.4
            elif stock == 'AAPL':
                results['total_return'] = 15.2
                results['win_rate'] = 72.1
            
            return jsonify({
                "status": "success",
                "message": f"Backtest completed for {stock}",
                "results": results
            })
        else:
            if pipeline_backtest:
                logger.info(f"Running production backtest for {stock}")
                results = pipeline_backtest.run(stock=stock)
                return jsonify({
                    "status": "success", 
                    "message": f"Backtest completed for {stock}",
                    "results": results
                })
            else:
                return jsonify({
                    "status": "error", 
                    "message": "Pipeline not initialized"
                }), 503
    except Exception as e:
        logger.error(f"Backtest error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# =================== MAIN APPLICATION ===================

if __name__ == '__main__':
    if DEVELOPMENT_MODE:
        logger.info("🔧 Starting ACA Trading Pipeline in DEVELOPMENT MODE")
        logger.info("📊 Using YFinance for real stock data")
    else:
        logger.info("🚀 Starting ACA Trading Pipeline in PRODUCTION MODE")
        logger.info("📡 Initializing trading pipeline...")
        # threading.Thread(target=initialize_pipeline, daemon=True).start()
    
    logger.info("-" * 60)
    
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)