Neste steg i PowerShell inne i klonet repo:

  cd C:\Users\TA487\code\bilder
  py -3.14 -m venv .venv
  .\.venv\Scripts\python -m pip install -e .
  .\.venv\Scripts\bdb --help
  .\.venv\Scripts\python -m unittest discover -v

  Når det fungerer, test med en liten bildesamling utenfor repoet:

  .\.venv\Scripts\bdb target C:\Users\TA487\BildeImportTest\target
  .\.venv\Scripts\bdb --target C:\Users\TA487\BildeImportTest\target add C:\Users\TA487\BildeImportTest\source
  .\.venv\Scripts\bdb --target C:\Users\TA487\BildeImportTest\target import
  .\.venv\Scripts\bdb --target C:\Users\TA487\BildeImportTest\target report

  Ikke bruk ekte hovedsamling før den lille testen ser riktig ut.
