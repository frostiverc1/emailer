aws_region  = "us-east-1"
name_prefix = "emailer"

# Each address needs to click the SES confirmation link before it can send.
ses_verified_emails = [
  "akhileshss991@gmail.com",
  "support@gencoft.com",
  "sa11052002@gmail.com"
]

alarm_email = "akhileshss991@gmail.com"

tags = {
  project = "emailer-pilot"
}
