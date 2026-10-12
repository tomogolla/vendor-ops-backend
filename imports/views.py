from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdministrator

from . import services


def _read_upload(request):
    upload = request.FILES.get('file')
    if upload is None:
        return None, Response({'file': 'Attach a CSV file.'}, status=status.HTTP_400_BAD_REQUEST)
    if upload.size > services.MAX_CSV_BYTES:
        return None, Response({'file': 'The file is larger than 2 MB.'}, status=status.HTTP_400_BAD_REQUEST)
    return upload, None


class ImportPreviewView(APIView):
    permission_classes = [IsAdministrator]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        upload, error = _read_upload(request)
        if error:
            return error
        try:
            return Response(services.preview(upload.read()))
        except services.CsvError as csv_error:
            return Response({'file': str(csv_error)}, status=status.HTTP_400_BAD_REQUEST)


class ImportConfirmView(APIView):
    permission_classes = [IsAdministrator]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        upload, error = _read_upload(request)
        if error:
            return error
        try:
            include = {int(line) for line in request.data.getlist('include_lines') if str(line).isdigit()}
            result = services.confirm(upload.read(), actor=request.user, file_name=upload.name, include_lines=include)
        except services.CsvError as csv_error:
            return Response({'file': str(csv_error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_201_CREATED)
