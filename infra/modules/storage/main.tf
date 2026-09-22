data "aws_caller_identity" "current" {}

# Holds email attachments between the client's upload and the worker sending them. Objects are
# deleted by the worker right after it sends (or gives up on) the email; this rule is only a
# backstop for anything that never gets cleaned up (a crashed worker, an abandoned upload).
# S3 bucket names are unique across all of AWS, not just this account, so the account ID is
# appended to avoid colliding with someone else's bucket.
resource "aws_s3_bucket" "attachments" {
  bucket = "${var.name_prefix}-attachments-${data.aws_caller_identity.current.account_id}"
  tags   = var.tags
}

# The browser (a customer's site, or this project's own dashboard) uploads straight to S3 with
# the presigned POST from validate, so the bucket itself needs to allow that cross-origin request
# — API Gateway's CORS config (in the api module) has no bearing on S3.
resource "aws_s3_bucket_cors_configuration" "attachments" {
  bucket = aws_s3_bucket.attachments.id
  cors_rule {
    allowed_methods = ["POST"]
    allowed_origins = ["*"]
    allowed_headers = ["*"]
    max_age_seconds = 3000
  }
}

resource "aws_s3_bucket_public_access_block" "attachments" {
  bucket                  = aws_s3_bucket.attachments.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "attachments" {
  bucket = aws_s3_bucket.attachments.id
  rule {
    id     = "expire-uploads"
    status = "Enabled"
    filter {
      prefix = "users/"
    }
    expiration {
      days = 1
    }
  }
}

resource "aws_dynamodb_table" "ops" {
  name         = "${var.name_prefix}-ops"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = var.tags
}
