// ==========================================================================
//  NIFTY 50 Weekly Options - Google Sheets Live Feed
// ==========================================================================
//
//  HOW TO USE:
//  1. Open your Google Sheet
//  2. Go to Extensions > Apps Script
//  3. Paste this entire code and Save
//  4. Run "setupSheet" first (this creates the layout)
//  5. Use the custom menu "NSE Options" > "Refresh Data" to fetch live data
//  6. Set up a time-based trigger for auto-refresh (optional)
//
//  Set SERVER_URL to your deployed API base URL (HTTPS recommended)
// ==========================================================================

// ==================== CONFIGURATION ====================
const SERVER_URL = "https://your-domain.example.com";  // Your deployed API URL
const NUM_STRIKES = 20;                       // Number of strikes above/below ATM
const REFRESH_INTERVAL_MINUTES = 1;           // Auto-refresh interval
const MARKET_START_HOUR = 9;                  // Market hours auto-start: 9:13 AM IST
const MARKET_START_MINUTE = 13;
const MARKET_END_HOUR = 15;                   // Market hours auto-stop: 3:35 PM IST
const MARKET_END_MINUTE = 35;
const SHEET_NAME = "NIFTY Options";           // Sheet name for option chain
const DASHBOARD_SHEET = "Dashboard";          // Sheet name for dashboard
const OHLC_SHEET = "OHLC History";            // Daily OHLC history sheet
const OHLC_LOG_SHEET = "Capture Log";         // OHLC capture log
// =======================================================


/**
 * Creates custom menu in Google Sheets
 */
function onOpen() {
  try {
    const ui = SpreadsheetApp.getUi();
    ui.createMenu('🔴 NSE Options')
      .addItem('📊 Refresh Data', 'refreshOptionChain')
      .addSeparator()
      .addItem('⚙️ Setup Sheet', 'setupSheet')
      .addItem('⏰ Start Auto-Refresh (1 min)', 'startAutoRefresh')
      .addItem('⏹️ Stop Auto-Refresh', 'stopAutoRefresh')
      .addSeparator()
      .addItem('🕘 Setup Market Hours Schedule (9:13–3:35)', 'setupMarketHoursSchedule')
      .addItem('🗑️ Remove Market Hours Schedule', 'removeMarketHoursSchedule')
      .addSeparator()
      .addItem('📥 Capture Daily OHLC', 'captureDailyOHLC')
      .addItem('⏰ Setup Daily 8:30 PM Trigger', 'setupDailyOHLCTrigger')
      .addItem('⏹️ Remove Daily Trigger', 'removeDailyOHLCTrigger')
      .addSeparator()
      .addItem('🔗 Test Server Connection', 'testConnection')
      .addToUi();
  } catch (e) {
    Logger.log('onOpen: No UI context (run from editor). Open the spreadsheet instead.');
  }
}


/**
 * Initial sheet setup - creates headers and formatting
 */
function setupSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  
  // Create or get Options sheet
  let optSheet = ss.getSheetByName(SHEET_NAME);
  if (!optSheet) {
    optSheet = ss.insertSheet(SHEET_NAME);
  }
  
  // Create or get Dashboard sheet
  let dashSheet = ss.getSheetByName(DASHBOARD_SHEET);
  if (!dashSheet) {
    dashSheet = ss.insertSheet(DASHBOARD_SHEET);
  }
  
  // Setup Dashboard
  setupDashboard(dashSheet);
  
  // Setup Options sheet headers
  setupOptionsSheet(optSheet);

  // Setup OHLC sheets
  setupOHLCSheets();

  // Setup automated triggers (idempotent — removes existing before creating)
  setupMarketHoursSchedule_silent();   // Auto-refresh: 9:13 AM – 3:35 PM IST Mon-Fri
  setupDailyOHLCTrigger_silent();      // OHLC capture: 8:30 PM IST daily
  
  // Only show alert when called directly from the menu (not from other functions)
  try {
    SpreadsheetApp.getUi().alert(
      '✅ Setup complete!\n\n' +
      '• Market hours auto-refresh: 9:13 AM – 3:35 PM IST (Mon-Fri)\n' +
      '• Daily OHLC capture: 8:30 PM IST\n\n' +
      'Use "NSE Options > Refresh Data" to fetch live data now.'
    );
  } catch (e) {
    // Called from trigger/other function — skip UI alert
    Logger.log('Setup complete (no UI context).');
  }
}


/**
 * Sets up the Dashboard sheet with metadata display
 */
function setupDashboard(sheet) {
  sheet.clear();
  sheet.setColumnWidths(1, 6, 180);
  
  // Title
  sheet.getRange('A1').setValue('🔴 NIFTY 50 WEEKLY OPTIONS - LIVE DASHBOARD');
  sheet.getRange('A1:F1').merge()
    .setFontSize(16).setFontWeight('bold')
    .setBackground('#1a237e').setFontColor('#ffffff')
    .setHorizontalAlignment('center');
  
  // Metadata labels
  const labels = [
    ['NIFTY Spot Price', '', 'ATM Strike', '', 'Last Updated', ''],
    ['Current Expiry', '', 'PCR (OI)', '', 'PCR (Volume)', ''],
    ['Total CE OI', '', 'Total PE OI', '', 'CE-PE OI Diff', ''],
    ['Total CE Volume', '', 'Total PE Volume', '', 'Server Status', ''],
    ['Market Sentiment', '', 'Max Pain', '', 'Days to Expiry', ''],
    ['OI Support', '', 'OI Resistance', '', 'Spot vs Max Pain', ''],
  ];
  
  sheet.getRange(3, 1, labels.length, 6).setValues(labels);
  
  // Format label columns
  for (let i = 3; i <= 8; i++) {
    sheet.getRange(i, 1).setFontWeight('bold').setBackground('#e3f2fd');
    sheet.getRange(i, 3).setFontWeight('bold').setBackground('#e3f2fd');
    sheet.getRange(i, 5).setFontWeight('bold').setBackground('#e3f2fd');
    sheet.getRange(i, 2).setFontWeight('bold').setFontSize(12);
    sheet.getRange(i, 4).setFontWeight('bold').setFontSize(12);
    sheet.getRange(i, 6).setFontWeight('bold').setFontSize(12);
  }
  
  // Sentiment Reasoning section
  sheet.getRange('A10').setValue('📊 Sentiment Analysis Breakdown');
  sheet.getRange('A10:F10').merge()
    .setFontSize(12).setFontWeight('bold')
    .setBackground('#e8eaf6');
  
  // Available Expiries section
  sheet.getRange('A21').setValue('📅 Available Expiry Dates');
  sheet.getRange('A21:F21').merge()
    .setFontSize(12).setFontWeight('bold')
    .setBackground('#e8eaf6');
}


/**
 * Sets up the Options chain sheet with headers
 */
function setupOptionsSheet(sheet) {
  sheet.clear();
  
  // Title row
  sheet.getRange('A1').setValue('NIFTY 50 WEEKLY OPTIONS CHAIN');
  sheet.getRange('A1:X1').merge()
    .setFontSize(14).setFontWeight('bold')
    .setBackground('#1a237e').setFontColor('#ffffff')
    .setHorizontalAlignment('center');
  
  // Info row
  sheet.getRange('A2').setValue('Loading...');
  sheet.getRange('A2:X2').merge()
    .setFontSize(10)
    .setBackground('#e8eaf6')
    .setHorizontalAlignment('center');
  
  // Headers (24 columns)
  const headers = [
    'Buildup',
    'OI', 'Chng OI', 'Volume', 'IV', 'LTP',
    'Change', 'Delta', 'Gamma', 'Theta', 'Vega',
    'STRIKE',
    'Delta', 'Gamma', 'Theta', 'Vega',
    'Change', 'LTP',
    'IV', 'Volume', 'Chng OI', 'OI',
    'Buildup', 'Signal'
  ];
  
  sheet.getRange(3, 1, 1, headers.length).setValues([headers]);
  
  // Format headers
  const headerRange = sheet.getRange(3, 1, 1, headers.length);
  headerRange.setFontWeight('bold')
    .setHorizontalAlignment('center')
    .setBackground('#263238')
    .setFontColor('#ffffff')
    .setFontSize(9);
  
  // CE Buildup header (col 1) - blue
  sheet.getRange(3, 1, 1, 1).setBackground('#1565c0').setFontColor('#ffffff');
  
  // CE data headers (cols 2-7) - green
  sheet.getRange(3, 2, 1, 6).setBackground('#2e7d32').setFontColor('#ffffff');
  
  // CE Greeks headers (cols 8-11) - teal
  sheet.getRange(3, 8, 1, 4).setBackground('#00695c').setFontColor('#ffffff');
  
  // Strike column (12) - dark
  sheet.getRange(3, 12, 1, 1).setBackground('#263238').setFontColor('#ffffff');
  
  // PE Greeks headers (cols 13-16) - teal
  sheet.getRange(3, 13, 1, 4).setBackground('#00695c').setFontColor('#ffffff');
  
  // PE data headers (cols 17-22) - red
  sheet.getRange(3, 17, 1, 6).setBackground('#c62828').setFontColor('#ffffff');
  
  // PE Buildup header (col 23) - blue
  sheet.getRange(3, 23, 1, 1).setBackground('#1565c0').setFontColor('#ffffff');
  
  // Signal header (col 24) - purple
  sheet.getRange(3, 24, 1, 1).setBackground('#6a1b9a').setFontColor('#ffffff');
  
  // Set column widths
  sheet.setColumnWidth(1, 110);  // CE Buildup
  for (let i = 2; i <= 7; i++) sheet.setColumnWidth(i, 85);   // CE data
  for (let i = 8; i <= 11; i++) sheet.setColumnWidth(i, 70);  // CE Greeks
  sheet.setColumnWidth(12, 80);  // Strike
  for (let i = 13; i <= 16; i++) sheet.setColumnWidth(i, 70); // PE Greeks
  for (let i = 17; i <= 22; i++) sheet.setColumnWidth(i, 85); // PE data
  sheet.setColumnWidth(23, 110); // PE Buildup
  sheet.setColumnWidth(24, 130); // Signal
  
  // Freeze header rows
  sheet.setFrozenRows(3);
}


/**
 * Main function - Fetches and displays NIFTY weekly options data
 */
function refreshOptionChain() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let optSheet = ss.getSheetByName(SHEET_NAME);
  let dashSheet = ss.getSheetByName(DASHBOARD_SHEET);
  
  if (!optSheet || !dashSheet) {
    setupSheet();
    optSheet = ss.getSheetByName(SHEET_NAME);
    dashSheet = ss.getSheetByName(DASHBOARD_SHEET);
  }
  
  try {
    // Fetch data from server
    const url = `${SERVER_URL}/api/options/sheets?strikes=${NUM_STRIKES}`;
    const response = UrlFetchApp.fetch(url, {
      muteHttpExceptions: true,
      headers: {
        'Accept': 'application/json'
      }
    });
    
    if (response.getResponseCode() !== 200) {
      throw new Error(`Server returned ${response.getResponseCode()}: ${response.getContentText()}`);
    }
    
    const json = JSON.parse(response.getContentText());
    
    if (json.status !== 'success') {
      throw new Error(json.error || 'Unknown error');
    }
    
    const metadata = json.metadata;
    const rows = json.rows;
    const signalNotes = json.signal_notes || {};
    
    // Update Dashboard
    updateDashboard(dashSheet, metadata);
    
    // Update Options Sheet
    updateOptionsSheet(optSheet, metadata, rows, signalNotes);
    
    // Flash notification
    try {
      SpreadsheetApp.getActiveSpreadsheet().toast(
        `Data refreshed at ${metadata.timestamp}`, 
        '✅ NIFTY Options Updated', 
        3
      );
    } catch (e) {
      Logger.log('Data refreshed at ' + metadata.timestamp);
    }
    
  } catch (error) {
    try {
      SpreadsheetApp.getActiveSpreadsheet().toast(
        error.message, 
        '❌ Error', 
        5
      );
    } catch (e) {
      // No UI context
    }
    Logger.log('Error: ' + error.message);
  }
}


/**
 * Updates the Dashboard sheet with latest metadata
 */
function updateDashboard(sheet, metadata) {
  // Row 3: Spot, ATM, Timestamp
  sheet.getRange('B3').setValue(metadata.spot_price)
    .setNumberFormat('#,##0.00').setFontColor('#1565c0');
  sheet.getRange('D3').setValue(metadata.atm_strike)
    .setNumberFormat('#,##0').setFontColor('#1565c0');
  sheet.getRange('F3').setValue(metadata.timestamp);
  
  // Row 4: Expiry, PCR
  sheet.getRange('B4').setValue(metadata.expiry).setFontColor('#d32f2f');
  sheet.getRange('D4').setValue(metadata.pcr_oi)
    .setNumberFormat('0.00')
    .setFontColor(metadata.pcr_oi > 1 ? '#2e7d32' : '#c62828');
  sheet.getRange('F4').setValue(metadata.pcr_volume)
    .setNumberFormat('0.00')
    .setFontColor(metadata.pcr_volume > 1 ? '#2e7d32' : '#c62828');
  
  // Row 5: OI totals
  sheet.getRange('B5').setValue(metadata.total_ce_oi).setNumberFormat('#,##0');
  sheet.getRange('D5').setValue(metadata.total_pe_oi).setNumberFormat('#,##0');
  sheet.getRange('F5').setValue(metadata.total_pe_oi - metadata.total_ce_oi)
    .setNumberFormat('#,##0');
  
  // Row 6: Volume totals
  sheet.getRange('B6').setValue(metadata.total_ce_volume).setNumberFormat('#,##0');
  sheet.getRange('D6').setValue(metadata.total_pe_volume).setNumberFormat('#,##0');
  sheet.getRange('F6').setValue('🟢 Online');
  
  // Row 7: Market Sentiment, Max Pain, Days to Expiry
  var sentiment = metadata.market_sentiment || 'N/A';
  var sentimentColor = sentiment === 'BULLISH' ? '#2e7d32' : sentiment === 'BEARISH' ? '#c62828' : '#f57f17';
  sheet.getRange('B7').setValue(sentiment).setFontColor(sentimentColor).setFontSize(14);
  
  var maxPain = metadata.max_pain || 0;
  sheet.getRange('D7').setValue(maxPain).setNumberFormat('#,##0').setFontColor('#6a1b9a');
  
  // Days to expiry
  if (metadata.expiry) {
    try {
      var parts = metadata.expiry.split('-');
      var expiryDate = new Date(parts[2], 'JanFebMarAprMayJunJulAugSepOctNovDec'.indexOf(parts[1]) / 3, parts[0]);
      var today = new Date();
      var dte = Math.max(0, Math.ceil((expiryDate - today) / (1000 * 60 * 60 * 24)));
      sheet.getRange('F7').setValue(dte + ' days').setFontColor(dte <= 2 ? '#c62828' : '#1565c0');
    } catch (e) {
      sheet.getRange('F7').setValue('N/A');
    }
  }
  
  // Row 8: OI Support, Resistance, Spot vs Max Pain
  var support = metadata.oi_support || [];
  var resistance = metadata.oi_resistance || [];
  sheet.getRange('B8').setValue(support.join(', ')).setFontColor('#2e7d32').setFontSize(11);
  sheet.getRange('D8').setValue(resistance.join(', ')).setFontColor('#c62828').setFontSize(11);
  
  if (maxPain > 0) {
    var diff = metadata.spot_price - maxPain;
    var diffText = diff > 0 ? 'Above MP by ' + Math.round(diff) : 'Below MP by ' + Math.abs(Math.round(diff));
    sheet.getRange('F8').setValue(diffText)
      .setFontColor(diff > 100 ? '#c62828' : diff < -100 ? '#2e7d32' : '#f57f17');
  }
  
  // Sentiment Reasons (rows 11-17)
  if (metadata.sentiment_reasons && metadata.sentiment_reasons.length > 0) {
    // Clear old
    sheet.getRange(11, 1, 10, 6).clearContent().clearFormat();
    var reasons = metadata.sentiment_reasons;
    for (var r = 0; r < Math.min(reasons.length, 10); r++) {
      sheet.getRange(11 + r, 1).setValue(reasons[r]);
      sheet.getRange(11 + r, 1, 1, 6).merge().setFontSize(9);
      if (r === 0) {
        sheet.getRange(11, 1).setFontWeight('bold').setFontSize(10)
          .setFontColor(sentiment === 'BULLISH' ? '#2e7d32' : sentiment === 'BEARISH' ? '#c62828' : '#f57f17');
      }
    }
  }
  
  // Expiry dates list (now starts at row 22)
  if (metadata.expiry_dates && metadata.expiry_dates.length > 0) {
    const expiries = metadata.expiry_dates.map(e => [e]);
    // Clear old expiry data
    if (sheet.getLastRow() > 21) {
      sheet.getRange(22, 1, sheet.getLastRow() - 21, 2).clear();
    }
    sheet.getRange(22, 1, expiries.length, 1).setValues(expiries);
    
    // Mark nearest expiry
    sheet.getRange(22, 2).setValue('⬅ Current Weekly');
  }
}


/**
 * Updates the Options chain sheet with live data
 */
function updateOptionsSheet(sheet, metadata, rows, signalNotes) {
  signalNotes = signalNotes || {};
  // Update info row
  const infoText = `NIFTY Spot: ${metadata.spot_price} | ATM: ${metadata.atm_strike} | ` +
    `Expiry: ${metadata.expiry} | PCR(OI): ${metadata.pcr_oi} | ` +
    `MaxPain: ${metadata.max_pain || 'N/A'} | S: ${(metadata.oi_support || []).join(',')} | R: ${(metadata.oi_resistance || []).join(',')} | ` +
    `Sentiment: ${metadata.market_sentiment || 'N/A'} | Updated: ${metadata.timestamp}`;
  sheet.getRange('A2:X2').merge();
  sheet.getRange('A2').setValue(infoText)
    .setFontSize(10)
    .setBackground('#e8eaf6')
    .setHorizontalAlignment('center');
  
  // Clear old data (keep headers) — 24 columns
  const lastRow = sheet.getLastRow();
  if (lastRow > 3) {
    sheet.getRange(4, 1, lastRow - 3, 24).clearContent();
    sheet.getRange(4, 1, lastRow - 3, 24).clearFormat();
    sheet.getRange(4, 1, lastRow - 3, 24).clearNote();
  }
  
  if (rows.length === 0) return;
  
  // Write data
  const dataRange = sheet.getRange(4, 1, rows.length, rows[0].length);
  dataRange.setValues(rows);
  
  // Format data
  dataRange.setHorizontalAlignment('center').setFontSize(9);
  
  // Number formatting (23 columns)
  // Col 1 = CE Buildup (text)
  sheet.getRange(4, 2, rows.length, 1).setNumberFormat('#,##0');   // CE OI
  sheet.getRange(4, 3, rows.length, 1).setNumberFormat('#,##0');   // CE Chng OI
  sheet.getRange(4, 4, rows.length, 1).setNumberFormat('#,##0');   // CE Volume
  sheet.getRange(4, 5, rows.length, 1).setNumberFormat('0.00');    // CE IV
  sheet.getRange(4, 6, rows.length, 1).setNumberFormat('#,##0.00');// CE LTP
  sheet.getRange(4, 7, rows.length, 1).setNumberFormat('+#,##0.00;-#,##0.00');// CE Change
  sheet.getRange(4, 8, rows.length, 1).setNumberFormat('0.0000');  // CE Delta
  sheet.getRange(4, 9, rows.length, 1).setNumberFormat('0.0000');  // CE Gamma
  sheet.getRange(4, 10, rows.length, 1).setNumberFormat('#,##0.00');// CE Theta
  sheet.getRange(4, 11, rows.length, 1).setNumberFormat('0.00');   // CE Vega
  sheet.getRange(4, 12, rows.length, 1).setNumberFormat('#,##0');  // Strike
  sheet.getRange(4, 13, rows.length, 1).setNumberFormat('0.0000'); // PE Delta
  sheet.getRange(4, 14, rows.length, 1).setNumberFormat('0.0000'); // PE Gamma
  sheet.getRange(4, 15, rows.length, 1).setNumberFormat('#,##0.00');// PE Theta
  sheet.getRange(4, 16, rows.length, 1).setNumberFormat('0.00');   // PE Vega
  sheet.getRange(4, 17, rows.length, 1).setNumberFormat('+#,##0.00;-#,##0.00');// PE Change
  sheet.getRange(4, 18, rows.length, 1).setNumberFormat('#,##0.00');// PE LTP
  sheet.getRange(4, 19, rows.length, 1).setNumberFormat('0.00');   // PE IV
  sheet.getRange(4, 20, rows.length, 1).setNumberFormat('#,##0');  // PE Volume
  sheet.getRange(4, 21, rows.length, 1).setNumberFormat('#,##0');  // PE Chng OI
  sheet.getRange(4, 22, rows.length, 1).setNumberFormat('#,##0');  // PE OI
  // Col 23 = PE Buildup (text)
  
  // Buildup color coding helper
  function getBuildupColors(buildup) {
    switch (buildup) {
      case 'Long Buildup':    return { bg: '#c8e6c9', fg: '#1b5e20' }; // Green
      case 'Short Buildup':   return { bg: '#ffcdd2', fg: '#b71c1c' }; // Red
      case 'Short Covering':  return { bg: '#fff9c4', fg: '#f57f17' }; // Yellow
      case 'Long Unwinding':  return { bg: '#ffe0b2', fg: '#e65100' }; // Orange
      default:                return { bg: '#ffffff', fg: '#757575' }; // Grey
    }
  }
  
  // Color coding for rows
  for (let i = 0; i < rows.length; i++) {
    const rowNum = i + 4;
    const strike = rows[i][11];   // Col 12 (index 11)
    const spotPrice = metadata.spot_price;
    const isATM = strike === metadata.atm_strike;
    
    // ATM row highlighting
    if (isATM) {
      sheet.getRange(rowNum, 1, 1, 24).setBackground('#fff9c4').setFontWeight('bold');
    }
    // ITM Call (strike < spot) - light green tint
    else if (strike < spotPrice) {
      sheet.getRange(rowNum, 2, 1, 10).setBackground('#e8f5e9');  // CE data + CE Greeks
      sheet.getRange(rowNum, 13, 1, 10).setBackground('#ffffff'); // PE Greeks + PE data
    }
    // ITM Put (strike > spot) - light red tint
    else if (strike > spotPrice) {
      sheet.getRange(rowNum, 2, 1, 10).setBackground('#ffffff');
      sheet.getRange(rowNum, 13, 1, 10).setBackground('#ffebee');
    }
    
    // Strike column always dark
    sheet.getRange(rowNum, 12, 1, 1).setBackground('#eceff1').setFontWeight('bold');
    
    // Color CE Change (col 7)
    const ceChange = rows[i][6];
    if (ceChange > 0) {
      sheet.getRange(rowNum, 7).setFontColor('#2e7d32');
    } else if (ceChange < 0) {
      sheet.getRange(rowNum, 7).setFontColor('#c62828');
    }
    
    // Color PE Change (col 17)
    const peChange = rows[i][16];
    if (peChange > 0) {
      sheet.getRange(rowNum, 17).setFontColor('#2e7d32');
    } else if (peChange < 0) {
      sheet.getRange(rowNum, 17).setFontColor('#c62828');
    }
    
    // CE Buildup coloring (col 1)
    const ceBU = getBuildupColors(rows[i][0]);
    sheet.getRange(rowNum, 1).setBackground(ceBU.bg).setFontColor(ceBU.fg).setFontWeight('bold').setFontSize(8);
    
    // PE Buildup coloring (col 23)
    const peBU = getBuildupColors(rows[i][22]);
    sheet.getRange(rowNum, 23).setBackground(peBU.bg).setFontColor(peBU.fg).setFontWeight('bold').setFontSize(8);
    
    // Signal coloring + note (col 24)
    const signal = rows[i][23] || '';
    const signalCell = sheet.getRange(rowNum, 24);
    if (signal.includes('Strong Buy CE')) {
      signalCell.setBackground('#1b5e20').setFontColor('#ffffff').setFontWeight('bold').setFontSize(9);
    } else if (signal.includes('Buy CE')) {
      signalCell.setBackground('#c8e6c9').setFontColor('#1b5e20').setFontWeight('bold').setFontSize(9);
    } else if (signal.includes('Strong Buy PE')) {
      signalCell.setBackground('#b71c1c').setFontColor('#ffffff').setFontWeight('bold').setFontSize(9);
    } else if (signal.includes('Buy PE')) {
      signalCell.setBackground('#ffcdd2').setFontColor('#b71c1c').setFontWeight('bold').setFontSize(9);
    }
    // Add reasoning as cell note (hover to see)
    const noteKey = String(i);
    if (signalNotes[noteKey]) {
      signalCell.setNote(signalNotes[noteKey]);
    }
  }
  
  // Add borders
  sheet.getRange(3, 1, rows.length + 1, 24).setBorder(
    true, true, true, true, true, true,
    '#bdbdbd', SpreadsheetApp.BorderStyle.SOLID
  );
}




/**
 * Tests connection to the Flask server
 */
function testConnection() {
  try {
    const response = UrlFetchApp.fetch(`${SERVER_URL}/api/health`, {
      muteHttpExceptions: true,
      headers: {
        'Accept': 'application/json'
      }
    });
    
    if (response.getResponseCode() === 200) {
      const data = JSON.parse(response.getContentText());
      SpreadsheetApp.getUi().alert(
        `✅ Server is ONLINE!\n\n` +
        `Server Time: ${data.server_time}\n` +
        `Cache Age: ${data.cache_age ? data.cache_age + 's' : 'No cached data'}`
      );
    } else {
      SpreadsheetApp.getUi().alert(
        `❌ Server returned error: ${response.getResponseCode()}\n${response.getContentText()}`
      );
    }
  } catch (error) {
    SpreadsheetApp.getUi().alert(
      `❌ Cannot connect to server!\n\n` +
      `URL: ${SERVER_URL}\n` +
      `Error: ${error.message}\n\n` +
      `Make sure:\n` +
      `1. The API container is running\n` +
      `2. Check: curl ${SERVER_URL}/api/health\n` +
      `3. SERVER_URL is correct in the script`
    );
  }
}


/**
 * Starts auto-refresh timer (manual or called by market hours trigger)
 */
function startAutoRefresh() {
  // Remove existing refresh triggers
  stopAutoRefresh();
  
  // Create new time-based trigger
  ScriptApp.newTrigger('refreshOptionChain')
    .timeBased()
    .everyMinutes(REFRESH_INTERVAL_MINUTES)
    .create();
  
  try {
    SpreadsheetApp.getUi().alert(
      `✅ Auto-refresh enabled!\n\nData will refresh every ${REFRESH_INTERVAL_MINUTES} minute(s).\n` +
      `Use "NSE Options > Stop Auto-Refresh" to disable.`
    );
  } catch (e) {
    // Called from trigger (no UI) — just log
    Logger.log('Auto-refresh started (from market hours trigger)');
  }
}


/**
 * Stops auto-refresh timer (manual or called by market hours trigger)
 */
function stopAutoRefresh() {
  const triggers = ScriptApp.getProjectTriggers();
  for (const trigger of triggers) {
    if (trigger.getHandlerFunction() === 'refreshOptionChain') {
      ScriptApp.deleteTrigger(trigger);
    }
  }
  
  try {
    SpreadsheetApp.getActiveSpreadsheet().toast('Auto-refresh stopped.', '⏹️ Stopped', 3);
  } catch (e) {
    Logger.log('Auto-refresh stopped (from market hours trigger)');
  }
}


/**
 * Sets up daily market hours schedule:
 * - Starts auto-refresh at 9:13 AM IST (Mon-Fri)
 * - Stops auto-refresh at 3:35 PM IST (Mon-Fri)
 * Run this ONCE — it persists until removed.
 */
function setupMarketHoursSchedule() {
  // Remove existing market hours triggers first
  removeMarketHoursSchedule();

  // Trigger to START refresh at 9:13 AM IST
  ScriptApp.newTrigger('marketOpen')
    .timeBased()
    .atHour(MARKET_START_HOUR)
    .nearMinute(MARKET_START_MINUTE)
    .everyDays(1)
    .inTimezone('Asia/Kolkata')
    .create();

  // Trigger to STOP refresh at 3:35 PM IST
  ScriptApp.newTrigger('marketClose')
    .timeBased()
    .atHour(MARKET_END_HOUR)
    .nearMinute(MARKET_END_MINUTE)
    .everyDays(1)
    .inTimezone('Asia/Kolkata')
    .create();

  SpreadsheetApp.getUi().alert(
    `✅ Market hours schedule set!\n\n` +
    `Auto-refresh starts: ${MARKET_START_HOUR}:${String(MARKET_START_MINUTE).padStart(2,'0')} AM IST\n` +
    `Auto-refresh stops:  ${MARKET_END_HOUR}:${String(MARKET_END_MINUTE).padStart(2,'0')} PM IST\n\n` +
    `Runs daily (skips weekends automatically).\n` +
    `Use "Remove Market Hours Schedule" to disable.`
  );
}


/**
 * Removes market hours schedule triggers
 */
function removeMarketHoursSchedule() {
  const triggers = ScriptApp.getProjectTriggers();
  for (const trigger of triggers) {
    const fn = trigger.getHandlerFunction();
    if (fn === 'marketOpen' || fn === 'marketClose') {
      ScriptApp.deleteTrigger(trigger);
    }
  }
  try {
    SpreadsheetApp.getActiveSpreadsheet().toast('Market hours schedule removed.', '🗑️ Removed', 3);
  } catch (e) {}
}


/**
 * Called by daily trigger at 9:13 AM IST — starts auto-refresh if weekday
 */
function marketOpen() {
  const now = new Date();
  const day = now.getDay(); // 0=Sun, 6=Sat
  if (day === 0 || day === 6) {
    Logger.log('marketOpen: Weekend — skipping');
    return;
  }
  Logger.log('marketOpen: Starting auto-refresh');
  // Remove any stale refresh triggers, then start fresh
  stopAutoRefresh();
  ScriptApp.newTrigger('refreshOptionChain')
    .timeBased()
    .everyMinutes(REFRESH_INTERVAL_MINUTES)
    .create();
  Logger.log('marketOpen: Auto-refresh started');
}


/**
 * Called by daily trigger at 3:35 PM IST — stops auto-refresh
 */
function marketClose() {
  Logger.log('marketClose: Stopping auto-refresh');
  stopAutoRefresh();
  Logger.log('marketClose: Auto-refresh stopped');
}


/**
 * Silent version of setupMarketHoursSchedule — called from setupSheet() (no UI alerts)
 */
function setupMarketHoursSchedule_silent() {
  removeMarketHoursSchedule();

  ScriptApp.newTrigger('marketOpen')
    .timeBased()
    .atHour(MARKET_START_HOUR)
    .nearMinute(MARKET_START_MINUTE)
    .everyDays(1)
    .inTimezone('Asia/Kolkata')
    .create();

  ScriptApp.newTrigger('marketClose')
    .timeBased()
    .atHour(MARKET_END_HOUR)
    .nearMinute(MARKET_END_MINUTE)
    .everyDays(1)
    .inTimezone('Asia/Kolkata')
    .create();

  Logger.log('Market hours schedule set: ' + MARKET_START_HOUR + ':' + MARKET_START_MINUTE + ' - ' + MARKET_END_HOUR + ':' + MARKET_END_MINUTE + ' IST');
}


// ==========================================================================
//  DAILY OHLC HISTORY CAPTURE
//  Captures Open, High, Low, Close for all strikes (run after 8 PM IST)
// ==========================================================================


/**
 * Sets up the OHLC History and Capture Log sheets
 */
function setupOHLCSheets() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  // --- OHLC History sheet ---
  let hist = ss.getSheetByName(OHLC_SHEET);
  if (!hist) {
    hist = ss.insertSheet(OHLC_SHEET);
  }

  // Only add headers if sheet is empty
  if (hist.getLastRow() === 0) {
    const headers = [
      'Date', 'Expiry', 'Strike',
      'CE Open', 'CE High', 'CE Low', 'CE Close', 'CE OI', 'CE Volume', 'CE IV',
      'PE Open', 'PE High', 'PE Low', 'PE Close', 'PE OI', 'PE Volume', 'PE IV'
    ];
    hist.getRange(1, 1, 1, headers.length).setValues([headers]);

    // Header formatting
    const hdr = hist.getRange(1, 1, 1, headers.length);
    hdr.setFontWeight('bold')
      .setBackground('#1a237e').setFontColor('#ffffff')
      .setHorizontalAlignment('center').setFontSize(10);

    // CE columns green, PE columns red
    hist.getRange(1, 4, 1, 7).setBackground('#2e7d32');
    hist.getRange(1, 11, 1, 7).setBackground('#c62828');

    hist.setFrozenRows(1);
    hist.setColumnWidth(1, 100);
    hist.setColumnWidth(2, 110);
    hist.setColumnWidth(3, 80);
    for (let i = 4; i <= 17; i++) hist.setColumnWidth(i, 90);
  }

  // --- Capture Log sheet ---
  let log = ss.getSheetByName(OHLC_LOG_SHEET);
  if (!log) {
    log = ss.insertSheet(OHLC_LOG_SHEET);
  }
  if (log.getLastRow() === 0) {
    const logHeaders = ['Date', 'Time', 'Expiry', 'Spot Price', 'Strikes Captured', 'Status'];
    log.getRange(1, 1, 1, logHeaders.length).setValues([logHeaders]);
    log.getRange(1, 1, 1, logHeaders.length)
      .setFontWeight('bold')
      .setBackground('#37474f').setFontColor('#ffffff')
      .setHorizontalAlignment('center');
    log.setFrozenRows(1);
  }
}


/**
 * Captures daily OHLC for all strikes and appends to OHLC History sheet.
 * Call after 8 PM IST when NSE publishes official close prices.
 */
function captureDailyOHLC() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  // Auto-setup sheets if they don't exist
  let hist = ss.getSheetByName(OHLC_SHEET);
  let log  = ss.getSheetByName(OHLC_LOG_SHEET);
  if (!hist || !log) {
    setupOHLCSheets();
    hist = ss.getSheetByName(OHLC_SHEET);
    log  = ss.getSheetByName(OHLC_LOG_SHEET);
  }

  try {
    // Duplicate check — skip if today's data already captured
    const today = Utilities.formatDate(new Date(), 'Asia/Kolkata', 'yyyy-MM-dd');
    const lastRow = hist.getLastRow();
    if (lastRow > 1) {
      const lastDate = hist.getRange(lastRow, 1).getValue();
      if (lastDate === today) {
        const msg = `Data for ${today} already captured. Skipping.`;
        Logger.log(msg);
        try { ss.toast(msg, '⚠️ Already Captured', 5); } catch (e) {}
        return;
      }
    }

    // Fetch OHLC data from server
    const url = `${SERVER_URL}/api/options/daily-ohlc`;
    const response = UrlFetchApp.fetch(url, {
      muteHttpExceptions: true,
      headers: {
        'Accept': 'application/json'
      }
    });

    if (response.getResponseCode() !== 200) {
      throw new Error(`Server returned ${response.getResponseCode()}: ${response.getContentText().substring(0, 200)}`);
    }

    const json = JSON.parse(response.getContentText());
    if (json.status !== 'success') {
      throw new Error(json.error || 'Unknown error');
    }

    const rows = json.rows;
    if (rows.length === 0) {
      throw new Error('No data returned from server');
    }

    // Append rows to OHLC History
    const startRow = hist.getLastRow() + 1;
    hist.getRange(startRow, 1, rows.length, rows[0].length).setValues(rows);

    // Number formatting
    hist.getRange(startRow, 3, rows.length, 1).setNumberFormat('#,##0');      // Strike
    hist.getRange(startRow, 4, rows.length, 4).setNumberFormat('#,##0.00');   // CE OHLC
    hist.getRange(startRow, 8, rows.length, 1).setNumberFormat('#,##0');      // CE OI
    hist.getRange(startRow, 9, rows.length, 1).setNumberFormat('#,##0');      // CE Volume
    hist.getRange(startRow, 10, rows.length, 1).setNumberFormat('0.00');      // CE IV
    hist.getRange(startRow, 11, rows.length, 4).setNumberFormat('#,##0.00');  // PE OHLC
    hist.getRange(startRow, 15, rows.length, 1).setNumberFormat('#,##0');     // PE OI
    hist.getRange(startRow, 16, rows.length, 1).setNumberFormat('#,##0');     // PE Volume
    hist.getRange(startRow, 17, rows.length, 1).setNumberFormat('0.00');      // PE IV
    hist.getRange(startRow, 1, rows.length, 17).setHorizontalAlignment('center').setFontSize(9);

    // Log the capture
    const logRow = [
      json.date,
      Utilities.formatDate(new Date(), 'Asia/Kolkata', 'HH:mm:ss'),
      json.expiry,
      json.spot_price,
      json.strike_count,
      '✅ Success'
    ];
    log.getRange(log.getLastRow() + 1, 1, 1, logRow.length).setValues([logRow]);

    const msg = `Captured ${rows.length} rows for ${json.date} (Expiry: ${json.expiry})`;
    Logger.log(msg);
    try { ss.toast(msg, '📥 OHLC Captured', 5); } catch (e) {}

  } catch (error) {
    // Log failure
    const today = Utilities.formatDate(new Date(), 'Asia/Kolkata', 'yyyy-MM-dd');
    const logRow = [
      today,
      Utilities.formatDate(new Date(), 'Asia/Kolkata', 'HH:mm:ss'),
      '', '', 0,
      '❌ ' + error.message
    ];
    log.getRange(log.getLastRow() + 1, 1, 1, logRow.length).setValues([logRow]);

    Logger.log('OHLC Error: ' + error.message);
    try { ss.toast(error.message, '❌ Error', 5); } catch (e) {}
  }
}


/**
 * Sets up a daily trigger at 8:30 PM IST for OHLC capture
 */
function setupDailyOHLCTrigger() {
  removeDailyOHLCTrigger();

  ScriptApp.newTrigger('captureDailyOHLC')
    .timeBased()
    .atHour(20)
    .nearMinute(30)
    .everyDays(1)
    .inTimezone('Asia/Kolkata')
    .create();

  try {
    SpreadsheetApp.getUi().alert(
      '✅ Daily trigger set!\n\n' +
      'OHLC data will be captured every day at ~8:30 PM IST.\n' +
      'NSE updates close prices after 8 PM.\n\n' +
      'Note: The server must be running when the trigger fires.'
    );
  } catch (e) {
    Logger.log('Daily OHLC trigger set for 8:30 PM IST.');
  }
}


/**
 * Removes the daily OHLC trigger
 */
function removeDailyOHLCTrigger() {
  const triggers = ScriptApp.getProjectTriggers();
  for (const t of triggers) {
    if (t.getHandlerFunction() === 'captureDailyOHLC') {
      ScriptApp.deleteTrigger(t);
    }
  }
  try {
    SpreadsheetApp.getActiveSpreadsheet().toast('Daily OHLC trigger removed.', '⏹️ Stopped', 3);
  } catch (e) {}
}


/**
 * Silent version of setupDailyOHLCTrigger — called from setupSheet() (no UI alerts)
 */
function setupDailyOHLCTrigger_silent() {
  removeDailyOHLCTrigger();

  ScriptApp.newTrigger('captureDailyOHLC')
    .timeBased()
    .atHour(20)
    .nearMinute(30)
    .everyDays(1)
    .inTimezone('Asia/Kolkata')
    .create();

  Logger.log('Daily OHLC trigger set for 8:30 PM IST.');
}
