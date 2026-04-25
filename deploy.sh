#!/bin/bash
# Later Text2Voice — Lambda Deployment Packager
#
# This script packages the agent into a ZIP file ready to upload to AWS Lambda.
#
# REQUIREMENTS (install these on your Mac first if you haven't):
#   brew install python3
#   pip3 install awscli     (optional — only needed if you want CLI deployment)
#
# HOW TO USE:
#   1. Open Terminal and navigate to this folder:
#        cd /path-to-directory/folder-name/"Later Text2Voice"
#   2. Make this script executable (one-time):
#        chmod +x deploy.sh
#   3. Run it:
#        ./deploy.sh
#   4. Upload the generated lambda_package.zip to your Lambda function:
#        AWS Console → Lambda → your function → Code → Upload from → .zip file

set -e  # Stop immediately if any command fails

echo "────────────────────────────────────────"
echo "  Later Text2Voice — Building Lambda ZIP"
echo "────────────────────────────────────────"

# Clean up any previous build
rm -rf package lambda_package.zip
mkdir package

echo "→ Installing Linux-compatible dependencies for Lambda..."
# IMPORTANT: Lambda runs on Amazon Linux (x86_64), not macOS.
# Without --platform, pip installs Mac binaries that crash on Lambda.
# These flags tell pip to fetch pre-built Linux wheels from PyPI instead.
pip3 install -r requirements.txt -t package/ \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  --quiet

echo "→ Copying agent code..."
cp lambda_function.py package/

echo "→ Zipping everything up..."
cd package
zip -r ../lambda_package.zip . --quiet
cd ..

echo ""
echo "✓ Done! lambda_package.zip is ready to upload."
echo ""
echo "Next step: Go to AWS Console → Lambda → your function"
echo "           → Code tab → Upload from → .zip file"
echo "           → Select lambda_package.zip"
echo ""
