from rest_framework.routers import DefaultRouter
from .views import InvoiceViewSet, PaymentViewSet, ExpenseViewSet, InvoiceExpenseViewSet, ExpenseCategoryViewSet, InvoiceSettingsViewSet

router = DefaultRouter()
router.register(r'invoices', InvoiceViewSet, basename='invoice')
router.register(r'payments', PaymentViewSet, basename='payment')
router.register(r'expenses', ExpenseViewSet, basename='expense')
router.register(r'invoice-expenses', InvoiceExpenseViewSet, basename='invoice-expense')
router.register(r'expense-categories', ExpenseCategoryViewSet, basename='expense-category')
router.register(r'invoice-settings', InvoiceSettingsViewSet, basename='invoice-settings')

urlpatterns = router.urls
