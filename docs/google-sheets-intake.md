# Google Sheets application intake

The authenticated endpoint is:

`POST https://ops.thegoodflea.com/api/v1/public/google-sheets/applications/`

It sends both form types through the normal application pipeline, including duplicate matching,
application bundling, discovery-call tasks, activity history, and application email automation.

## Apps Script

In Apps Script, open **Project Settings → Script properties** and add:

- `VENDOR_OPS_WEBHOOK_URL`: `https://ops.thegoodflea.com/api/v1/public/google-sheets/applications/`
- `VENDOR_OPS_WEBHOOK_SECRET`: the same random value as the backend setting
  `GOOGLE_SHEETS_WEBHOOK_SECRET`

Use an installable **From spreadsheet → On form submit** trigger for this function:

```javascript
function onFormSubmitTrigger(e) {
  if (!e || !e.range || !e.source) {
    console.log("No form-submit event was supplied.");
    return;
  }

  const properties = PropertiesService.getScriptProperties();
  const webhookUrl = properties.getProperty("VENDOR_OPS_WEBHOOK_URL");
  const webhookSecret = properties.getProperty("VENDOR_OPS_WEBHOOK_SECRET");
  if (!webhookUrl || !webhookSecret) {
    throw new Error("VENDOR_OPS_WEBHOOK_URL and VENDOR_OPS_WEBHOOK_SECRET must be set in Script properties.");
  }

  const sheet = e.range.getSheet();
  const sheetName = sheet.getName();
  const namedValues = e.namedValues || {};

  function getValue(key) {
    return namedValues[key] && namedValues[key][0]
      ? namedValues[key][0].toString().trim()
      : "";
  }

  // Stable for this response row, so rerunning the trigger does not duplicate an application.
  const submissionId = [e.source.getId(), sheet.getSheetId(), e.range.getRow()].join(":");
  let payload;

  if (sheetName === "Vendor Interest Form") {
    payload = {
      submission_id: submissionId,
      sheet_type: "vendor_interest_form",
      first_name: getValue("First name"),
      business_name: getValue("Business name"),
      phone_number: getValue("Phone number"),
      instagram: getValue("Instagram"),
      email: getValue("email"),
      form_number: getValue("Form Number"),
      date_applied: getValue("Date applied")
    };
  } else if (sheetName === "popup form") {
    payload = {
      submission_id: submissionId,
      sheet_type: "popup_form",
      submission_date: getValue("submission_date"),
      first_name: getValue("first_name"),
      email: getValue("email"),
      brand_name: getValue("brand_name"),
      phone_number: getValue("phone_number")
    };
  } else {
    return;
  }

  const response = UrlFetchApp.fetch(webhookUrl, {
    method: "post",
    contentType: "application/json",
    headers: { "X-Webhook-Secret": webhookSecret },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });

  const code = response.getResponseCode();
  if (code < 200 || code >= 300) {
    throw new Error("Vendor Ops webhook returned HTTP " + code + ": " + response.getContentText());
  }
  console.log("Vendor Ops accepted " + submissionId + ": " + response.getContentText());
}
```

## Backend deployment

Generate a secret locally with `python -c "import secrets; print(secrets.token_urlsafe(48))"`, add it
as `GOOGLE_SHEETS_WEBHOOK_SECRET` in `/etc/vendor-ops/backend.env`, deploy the backend (which runs the
new migration), and put the same value in Apps Script properties. Never put it directly in the
script or commit it.
